#!/usr/bin/env python3
import os

import requests
import tarfile
import glob
import shutil
from github_connect import GitHub
from functools import partial
from itertools import chain, groupby
from multiprocessing import cpu_count
from multiprocessing.pool import ThreadPool as Pool
from os import sep, getenv, makedirs
from os.path import isdir, sys, dirname, normpath


def download_from_repo(github_token, org, repo, branch, globs, extract_dir, commit_sha=None):
    """
    Takes github info and file globs and downloads files from github using multiple processes
    :param github_token: A valide Github token to download content with the Github Class
    :param org: github organization or person
    :param repo: github repo name
    :param branch: the branch name
    :param globs: list of strings in glob format of what to extract
    :param extract_dir: Directory in which to put all downloaded content.
    :param commit_sha: sha if we want to provide one
    :return:
    """
    pool_size = cpu_count()

    with GitHub(github_token) as gh:
        listing = gh.list(org, repo, branch, commit_sha, globs)
        dest = "{0}{1}{2}".format(
            extract_dir, repo, sep
        )
        with Pool(processes=pool_size) as pool:
            with requests.Session() as s:
                r = [
                    x
                    for x in pool.imap_unordered(
                        partial(
                            gh.raw,
                            request_session=s,
                            org=org,
                            repo=repo,
                            branch=branch if not commit_sha else commit_sha,
                            dest_dir=dest,
                        ),
                        listing,
                    )
                ]


def download_from_local_repo(local_repo_path, org, repo, branch, globs, extract_dir, commit_sha=None):
    for path_to_file in chain.from_iterable(glob.glob('{}{}{}'.format(local_repo_path, sep, pattern), recursive=True) for pattern in globs):
        dest = "{0}{1}{2}{3}".format(extract_dir, repo, sep, normpath(path_to_file.replace(local_repo_path, '')))
        makedirs(dirname(dest), exist_ok=True)
        shutil.copyfile(path_to_file, dest)


def update_globs(new_path, globs):
    """
    Depending if the repo is local or we downloaded it we need to update the globs to match
    the final version of the repo to use
    :param new_path: new_path to update the globs with
    :param globs: list of globs to update
    """
    new_globs = []
    for item in globs:
        new_globs.append(os.path.join(new_path, item))
    return new_globs


def grouped_globs_table(list_of_contents):
    data = {}
    sorted_list_of_contents = sorted(list_of_contents, key=lambda k: k['repo_name'])
    for key, value in groupby(sorted_list_of_contents, lambda k: k['repo_name']):
        grouped_globs = [x['globs'] for x in value]
        data[key] = list(chain.from_iterable(grouped_globs))
    return data


def download_content_from_external_source(self, content):
    """
    Returns a boolean determining whether pull_config content should be downloaded from cache or external source.
    """
    use_cached = content.get('options', {}).get('cached', False)
    action = content.get('action', '')

    return (self.global_cache_enabled == False) \
        or (use_cached == False) \
        or (action in ('integrations', 'marketplace-integrations') and self.integrations_cache_enabled == False) \
        or (not getenv("CI_COMMIT_REF_NAME"))


def local_or_upstream(self, github_token, extract_dir, list_of_contents):
    """
    This goes through the list_of_contents and check for each repo specified in order:
      * [ONLY LOCAL DEV] Check if a locally cloned version is on this developer machine; one level above this documentation repo
      * [ONLY LOCAL DEV] Check if this docs build has already pulled and stored the repos in an extract folder
      * [LOCAL DEV AND CI] If neither of the above exist, pull the remote repo to use and store in the extract folder
    :param github_token: A valide Github token to download content with the Github Class
    :param extract_dir: Directory into which to put all content downloaded.
    :param list_of_content: List of content to check if available locally or if it needs to be downloaded from Github
    """
    grouped_globs = grouped_globs_table(list_of_contents)
    is_in_ci = os.getenv("CI_COMMIT_REF_NAME")

    for content in list_of_contents:
        if download_content_from_external_source(self, content):
            print(f'Downloading content from {content["repo_name"]}')
            local_repo_path = os.path.join("..", content["repo_name"])
            repo_path_last_extract = os.path.join(extract_dir, content["repo_name"])

            if isdir(local_repo_path) and not is_in_ci:
                print(f"\x1b[32mINFO\x1b[0m: Local version of {content['repo_name']} found in: {local_repo_path}")
                download_from_local_repo(local_repo_path, content["org_name"], content["repo_name"], content["branch"], grouped_globs.get(content["repo_name"], content["globs"]),
                                        extract_dir, content.get("sha", None))
                content["globs"] = update_globs(
                    "{0}{1}{2}".format(
                        extract_dir,
                        content["repo_name"],
                        sep,
                    ),
                    content["globs"],
                )
            elif isdir(repo_path_last_extract) and not is_in_ci:
                print(
                    f"\x1b[32mINFO\x1b[0m: Local version of {content['repo_name']} found from previous extract in:"
                    f" {repo_path_last_extract} "
                )
                content["globs"] = update_globs(
                    repo_path_last_extract,
                    content["globs"],
                )
            elif github_token != "false":
                print(
                    f"\x1b[32mINFO\x1b[0m: No local version of {content['repo_name']} found, downloading content from "
                    f"upstream version and placing in: {extract_dir}"
                )
                download_from_repo(github_token,
                                content["org_name"],
                                content["repo_name"],
                                content["branch"],
                                grouped_globs.get(content["repo_name"], content["globs"]),
                                extract_dir,
                                content.get("sha", None)
                                )
                content[
                    "globs"
                ] = update_globs(
                    "{0}{1}{2}".format(
                        extract_dir,
                        content["repo_name"],
                        sep,
                    ),
                    content["globs"],
                )
            elif getenv("LOCAL") == 'True':
                print(
                    "\x1b[33mWARNING\x1b[0m: No local version of {} found, no GITHUB_TOKEN available. Documentation is now in degraded mode".format(content["repo_name"]))
                content["action"] = "Not Available"
            else:
                print(
                    "\x1b[31mERROR\x1b[0m: No local version of {} found, no GITHUB_TOKEN available.".format(
                        content["repo_name"]
                    )
                )
                raise ValueError

    return list_of_contents


def extract_config(configuration):
    """
    This pulls the content from the configuration file at `configuration` location
    then parses it to populate the list_of_content variable that contains all contents
    that needs to be pulled and processed.
    :param configuration: Documentation build configuration file path.
    """
    list_of_contents = []
    content_configuration_data = configuration[1].get('data', {})

    for org in content_configuration_data:
        for repo in org["repos"]:
            for content in repo["contents"]:
                content_temp = {}
                content_temp["org_name"] = org[
                    "org_name"
                ]
                content_temp["repo_name"] = repo[
                    "repo_name"
                ]
                content_temp["branch"] = content[
                    "branch"
                ]
                content_temp["sha"] = content.get("sha", None)
                content_temp["action"] = content[
                    "action"
                ]
                content_temp["globs"] = content["globs"]

                if content["action"] in ("pull-and-push-folder", "pull-and-push-file", "security-rules", "compliance-rules", "workflows"):
                    content_temp["options"] = content["options"]

                list_of_contents.append(
                    content_temp
                )

    return list_of_contents


def prepare_content(self, configuration, github_token, extract_dir):
    """
    Prepares the content for the documentation build. It checks for all content whether or
    not it's available locally or if it should be downloaded.
    :param configuration: Documentation build configuration file path.
    :param github_token: A valide Github token to download content with the Github Class
    :param extract_dir: Directory into which to put all content downloaded.
    """
    try:
        list_of_contents = local_or_upstream(
            self, github_token, extract_dir, extract_config(configuration))
    except Exception as e:
        if not getenv("CI_COMMIT_REF_NAME"):
            print(
                f"\x1b[33mWARNING\x1b[0m: Downloading files failed, documentation is now in degraded mode. {e}")
        else:
            print(
                "\x1b[31mERROR\x1b[0m: Downloading files failed, stopping build.")
            sys.exit(1)
    return list_of_contents


def download_and_extract_cached_content():
    latest_commit_hash = getenv("LATEST_REV_HASH")
    static_bucket = getenv("STATIC_BUCKET")

    s3_url = f'https://{static_bucket}.s3.amazonaws.com/build_artifacts/master/{latest_commit_hash}-ignored.tar.gz'
    print(s3_url)
    artifact_download_response = requests.get(s3_url, stream=True)
    print(artifact_download_response)

    with tarfile.open(mode='r|gz', fileobj=artifact_download_response.raw) as artifact_tarfile:
        artifact_tarfile.extractall('temp')
        artifact_tarfile.close()


def copy_cached_content_into_repo(self, cached_content_array):
    """
    :param cached_content_array:
    """
    try:
        download_and_extract_cached_content()

        # Copy the files we want to use from the cache
        for content in cached_content_array:
            action = content.get('action', '')
            destination = ''

            if action == 'pull-and-push-file':
                dest_path = content.get('options', {}).get('dest_path')
                dest_file_name = content.get('options', {}).get('file_name')
                full_dest_path = f'{self.en_content_path}{dest_path}{dest_file_name}'

                os.makedirs(os.path.dirname(full_dest_path), exist_ok=True)
                print(f'Copying {full_dest_path} from cache')
                shutil.copy(f'temp/{full_dest_path}', full_dest_path)
            elif action == 'pull-and-push-folder':
                destination = content.get('options', {}).get('dest_dir', '')
            elif action in ('workflows', 'security-rules'):
                destination = content.get('options', {}).get('dest_path', '')
            elif action not in ('integrations', 'marketplace-integrations'):
                print(f'Action {action} unsupported, cannot copy from cache.')

            if destination != '':
                print(f'Copying {self.en_content_path}{destination} directory from cache')
                shutil.copytree(f'temp/{self.en_content_path}{destination}', f'{self.en_content_path}{destination}', dirs_exist_ok=True)

        # Integrations are handled separately for now
        if self.integrations_cache_enabled:
            shutil.copytree(f'temp/{self.en_content_path}/integrations', f'{self.en_content_path}/integrations', dirs_exist_ok=True)
            
        shutil.rmtree('temp')
    except Exception as e:
        print('Error copying cached content')
        print(e)
        shutil.rmtree('temp')
