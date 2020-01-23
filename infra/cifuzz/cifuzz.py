# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Module used by CI tools in order to interact with fuzzers.
This module helps CI tools do the following:
  1. Build fuzzers.
  2. Run fuzzers.
Eventually it will be used to help CI tools determine which fuzzers to run.
"""

import argparse
import enum
import logging
import os
import shutil
import sys

import fuzz_target

# pylint: disable=wrong-import-position
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build_specified_commit
import helper
import repo_manager
import utils


class Status(enum.Enum):
  """An Enum to store the possible return codes of the cifuzz module."""
  SUCCESS = 0
  ERROR = 1
  BUG_FOUND = 2


logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout,
    level=logging.DEBUG)


def main():
  """Connects fuzzers with CI tools.

  Returns:
    0 on success and 1 on failure.
  """
  parser = argparse.ArgumentParser(
      description='Help CI tools manage specific fuzzers.')

  subparsers = parser.add_subparsers(dest='command')
  build_fuzzer_parser = subparsers.add_parser(
      'build_fuzzers', help='Build an OSS-Fuzz projects fuzzers.')
  build_fuzzer_parser.add_argument('project_name')
  build_fuzzer_parser.add_argument('github_repo_name')
  build_fuzzer_parser.add_argument('commit_sha')

  run_fuzzer_parser = subparsers.add_parser(
      'run_fuzzers', help='Run an OSS-Fuzz projects fuzzers.')
  run_fuzzer_parser.add_argument('project_name')
  run_fuzzer_parser.add_argument('fuzz_seconds', type=int)
  args = parser.parse_args()

  # Get the shared volume directory and creates required directory.
  if 'GITHUB_WORKSPACE' not in os.environ:
    return Status.ERROR.value
  git_workspace = os.path.join(os.environ['GITHUB_WORKSPACE'], 'storage')
  if not os.path.exists(git_workspace):
    os.mkdir(git_workspace)
  out_dir = os.path.join(os.environ['GITHUB_WORKSPACE'], 'out')
  if not os.path.exists(out_dir):
    os.mkdir(out_dir)

  # Change to oss-fuzz main directory so helper.py runs correctly.
  if os.getcwd() != helper.OSSFUZZ_DIR:
    os.chdir(helper.OSSFUZZ_DIR)

  if args.command == 'build_fuzzers':
    if build_fuzzers(args, git_workspace, out_dir):
      return Status.SUCCESS.value
    return Status.ERROR.value
  if args.command == 'run_fuzzers':
    run_success, bug_found = run_fuzzers(args, out_dir)
    if bug_found:
      return Status.BUG_FOUND.value
    if run_success:
      return Status.SUCCESS.value
  return Status.ERROR.value


def build_fuzzers(args, git_workspace, out_dir):
  """Builds all of the fuzzers for a specific OSS-Fuzz project.

  Args:
    args: List of args passed in to cifuzz.py build_fuzzers parser.
    git_workspace: The location in the shared volume to store git repos.
    out_dir: The location in the shared volume to store output artifacts.

  Returns:
    True if build succeeded or False on failure.
  """
  # TODO: Modify build_specified_commit function to return src dir.

  inferred_url, oss_fuzz_repo_name = build_specified_commit.detect_main_repo(
      args.project_name, repo_name=args.github_repo_name)
  src = utils.get_env_var(args.project_name, 'SRC')
  if not src:
    logging.error('Could not get $SRC from project docker image. ')
    return False

  if not inferred_url or not oss_fuzz_repo_name:
    logging.error('Error: Repo URL or name could not be determined.')

  # Checkout projects repo in the shared volume.
  build_repo_manager = repo_manager.RepoManager(inferred_url,
                                                git_workspace,
                                                repo_name=oss_fuzz_repo_name)
  try:
    build_repo_manager.checkout_commit(args.commit_sha)
  except repo_manager.RepoManagerError:
    logging.error('Error: Specified commit does not exist.')
    # WARNING: Remove when done testing
    #return False

  command = ['--cap-add', 'SYS_PTRACE', '--volumes-from', utils.get_container()]
  command.extend([
      '-e', 'FUZZING_ENGINE=libfuzzer', '-e', 'SANITIZER=address', '-e',
      'ARCHITECTURE=x86_64', '-e', 'OUT=' + out_dir
  ])

  command.extend([
      'gcr.io/oss-fuzz/%s' % args.project_name,
      '/bin/bash',
      '-c',
  ])
  bash_command = 'rm -rf {0} && cp -r {1} {2} && compile'.format(
      os.path.join(src, oss_fuzz_repo_name, '*'),
      os.path.join(git_workspace, '.'), src)
  command.append(bash_command)

  if helper.docker_run(command):
    logging.error('Error: Building fuzzers failed.')
    return False
  return True


def run_fuzzers(args, out_dir):
  """Runs a all fuzzers for a specific OSS-Fuzz project.

  Args:
    args: List of args passed in to cifuzz.py run_fuzzers parser.
    out_dir: The location in the shared volume to store output artifacts.

  Returns:
    (True if run was successful, True if error was found False if not).
  """
  fuzzer_paths = utils.get_fuzz_targets(out_dir)
  if not fuzzer_paths:
    logging.error('Error: No fuzzers were found in out directory.')
    return False, False

  fuzzer_timeout = int(args.fuzz_seconds / len(fuzzer_paths))
  fuzz_targets = [
      fuzz_target.FuzzTarget(args.project_name, fuzzer_path, fuzzer_timeout)
      for fuzzer_path in fuzzer_paths
  ]

  for target in fuzz_targets:
    test_case, stack_trace = target.start()
    if not test_case or not stack_trace:
      logging.debug('Fuzzer %s, finished running.', target.target_name)
    else:
      logging.debug("Fuzzer %s, Detected Error: %s", target.target_name,
                    stack_trace)
      shutil.move(test_case, os.path.join(out_dir, 'testcase'))
      return True, True
  return True, False


if __name__ == '__main__':
  sys.exit(main())
