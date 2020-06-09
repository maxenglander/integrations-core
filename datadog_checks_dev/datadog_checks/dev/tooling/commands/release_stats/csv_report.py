# (C) Datadog, Inc. 2018-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
import click
import re
import datetime
import csv

from collections import namedtuple
from pathlib import Path

from ....utils import write_file_lines
from ..console import CONTEXT_SETTINGS, echo_failure, echo_info, echo_success
from ...github import get_compare, parse_pr_number, get_pr_from_hash, get_tags, get_tag, get_pr_of_repo, get_pr_labels, get_commit

class PullRequest:
    def __init__(self, number, title, url, labels):
        self.number = number
        self.title = title
        self.url = url
        self.labels = labels

    @classmethod
    def from_github_pr(cls, gh_pr):
        return PullRequest(
            number=gh_pr['number'],
            title=gh_pr['title'],
            url=gh_pr['html_url'],
            labels=get_pr_labels(gh_pr)
        )

    @classmethod
    def from_github(cls, ctx, number):
        gh_pr = get_pr_of_repo(number, 'datadog-agent', config=ctx.obj)

        return PullRequest.from_github_pr(gh_pr)

    @classmethod
    def from_commit(cls, ctx, commit_message, commit_sha):
        pr_number = parse_pr_number(commit_message)

        if pr_number is None:
            echo_info(f"Could not parse PR number from commit '{commit_sha}' - Will look for an issue with it...")
            found_items = get_pr_from_hash(commit_sha, 'datadog-agent', config=ctx.obj)['items']

            if len(found_items) == 0:
                echo_failure(f"Could not find relevant PR of commit '{commit_sha}'")
                return None

            pr_number = found_items[0]['number']
            echo_info(f"Found issue '{pr_number}'")

        return PullRequest.from_github(ctx, pr_number)

class Commit:
    def __init__(self, sha, title, date, author, pull_request, included_in_tag=None):
        self.sha = sha
        self.title = title
        self.date = date
        self.author = author
        self.pull_request = pull_request
        self.url = f'https://www.github.com/DataDog/datadog-agent/commit/{sha}'
        self.included_in_tag = included_in_tag

    def included_in(self, next_tag):
        self.included_in_tag = next_tag

    @classmethod
    def from_github_commit(cls, ctx, gh_commit):
        pull_request = PullRequest.from_commit(ctx, gh_commit['commit']['message'], gh_commit['sha'])

        return Commit(
            sha=gh_commit['sha'],
            title=gh_commit['commit']['message'],
            date=datetime.datetime.strptime(gh_commit['commit']['committer']['date'], '%Y-%m-%dT%H:%M:%SZ'),
            author=f"@{gh_commit['committer']['login']} - {gh_commit['commit']['committer']['name']}",
            pull_request=pull_request
        )

    @classmethod
    def from_github_compare(cls, ctx, from_ref, to_ref):
        comparison = get_compare(from_ref, to_ref, 'datadog-agent', config=ctx.obj)

        return [ Commit.from_github_commit(ctx, gh_commit) for gh_commit in comparison['commits'] ]

    @classmethod
    def get(cls, ctx, sha):
        gh_commit = get_commit('datadog-agent', sha, config=ctx.obj)

        return Commit.from_github_commit(ctx, gh_commit)


class Tag:
    def __init__(self, ref, sha, commit_sha=None):
        self.ref = ref
        self.name = ref.rpartition('/')[-1]
        self.sha = sha
        self.commit_sha = commit_sha

    def reload(self, ctx):
        tag = Tag.get(ctx, self.sha)

        self.ref = tag.ref
        self.name = tag.name
        self.sha = tag.sha
        self.commit_sha = tag.commit_sha

        return self

    @classmethod
    def from_github_tag(cls, gh_tag):
        if gh_tag['object']['type'] == 'commit':
            return Tag(
                ref=gh_tag.get('ref', gh_tag.get('tag')),
                sha=gh_tag.get('sha', None),
                commit_sha=gh_tag['object']['sha']
            )
        else:
            return Tag(
                ref=gh_tag['ref'],
                sha=gh_tag['object']['sha']
            )

    @classmethod
    def get(cls, ctx, tag_sha):
        return Tag.from_github_tag(get_tag('datadog-agent', tag_sha, config=ctx.obj))

    @classmethod
    def list_from_github(cls, ctx):
        gh_tags = get_tags('datadog-agent', config=ctx.obj)
        return [ Tag.from_github_tag(gh_tag) for gh_tag in gh_tags ]


class Release:
    def __init__(self, release_version, commits, rc_tags):
        self.release_version = release_version
        self.commits = commits
        self.rc_tags = rc_tags

    @classmethod
    def from_github(cls, ctx, release_version, from_ref, to_ref):
        # this will only contain agent 7 or 6 RCs but it's okay
        # as we want versions to assign commits / count them
        version_re = re.compile(f'{release_version}-rc')
        # comparing using provided references but using parent commit of from-reference to
        # also have the from-ref commit included
        echo_info(f'Fetching commits using compare from parent of "{from_ref}" to "{to_ref}"...')
        commits = Commit.from_github_compare(ctx, f'{from_ref}^', to_ref)
        echo_info(f'Fetching tags matching "/{version_re}/"...')
        rc_tags = [ tag for tag in Tag.list_from_github(ctx) if version_re.match(tag.name) or tag.name == release_version ]

        for rc_tag in rc_tags:
            # we are forced to reload tags as the github does not return the tag's commit's SHA
            # when we are using the tag list API
            if rc_tag.commit_sha is None:
                echo_info(f'Reloading tag "{rc_tag.name}" as it does not have a commit SHA')
                rc_tag.reload(ctx)

        echo_info(f'Assigning release candidates to commits...')
        # assign commits to release candidates
        tag_index = 0

        for commit in commits:
            # break if we cannot assign tags to commits anymore
            if tag_index >= len(rc_tags):
                echo_failure("Could not assign a tag to every commits")
                break

            commit.included_in(rc_tags[tag_index])

            if commit.sha == rc_tags[tag_index].commit_sha:
                tag_index += 1

        return Release(
            release_version=release_version,
            commits=commits,
            rc_tags=rc_tags
        )

class ReportSerializer:
    def __init__(self, release):
        self.release = release

    def write_report(self, filepath):
        with open(filepath, 'w', newline='') as csvfile:
            report = self._report()

            fieldnames = report.keys()
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

            writer.writeheader()
            writer.writerow(report)

    def write_changes(self, filepath):
        with open(filepath, 'w', newline='') as csvfile:
            changes = [ self._change(commit) for commit in self.release.commits ]
            fieldnames = changes[0].keys()
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()

            for change in changes:
                writer.writerow(change)

    def _report(self):
        return {
            'Release Branch': self.release.release_version,
            'Release candidates': len(self.release.rc_tags),
            'Number of Commits': len(self.release.commits),
            'Commits with unknown PR': len([commit for commit in self.release.commits if commit.pull_request is None ]),
            'Release time (days)': self._release_delay()
        }

    def _release_delay(self):
        rc_1 = self.release.commits[0]
        last_change = self.release.commits[-1]

        duration = (last_change.date - rc_1.date).total_seconds()

        return divmod(duration, 24 * 60 * 60)[0]

    def _change(self, commit):
        teams = []
        title = commit.title
        url = commit.url
        next_tag = None

        pull_request = commit.pull_request

        if pull_request:
            teams = [ label.rpartition('/')[-1] for label in pull_request.labels if label.startswith('team') ]
            title = pull_request.title
            url = pull_request.url

        if commit.included_in_tag:
            next_tag = commit.included_in_tag.name

        return {
            'SHA': commit.sha,
            'Title': title,
            'URL': url,
            'Teams': ' & '.join(teams),
            'Next tag': next_tag
        }


@click.command(
    context_settings=CONTEXT_SETTINGS,
    short_help="Writes the CSV report about a specific release",
)
@click.option('--from-ref', '-f', help="Reference to start stats on", required=True)
@click.option('--to-ref', '-t', help="Reference to end stats at", required=True)
@click.option('--release-version', '-r', help="Release version to analyze", required=True)
@click.option('--output-folder', '-o', help="Path to output folder")
@click.pass_context
def csv_report(ctx, from_ref, to_ref, release_version, output_folder=None):
    """Computes the release report and writes it to a specific directory
    """
    if output_folder is None:
        output_folder = branch

    folder = Path(output_folder)

    folder.mkdir(parents=True, exist_ok=True)

    release = Release.from_github(ctx, release_version, from_ref=from_ref, to_ref=to_ref)

    serializer = ReportSerializer(release)

    serializer.write_report(folder.joinpath('release.csv'))
    serializer.write_changes(folder.joinpath('changes.csv'))

    echo_success(f'Successfully wrote reports to directory `{output_folder}`')


