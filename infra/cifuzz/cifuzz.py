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


def build_fuzzers(project_name, project_repo_name, commit_sha, git_workspace,
                  out_dir):
  """Builds all of the fuzzers for a specific OSS-Fuzz project.

  Args:
    project_name: The name of the OSS-Fuzz project being built.
    project_repo_name: The name of the projects repo.
    commit_sha: The commit SHA to be checked out and fuzzed.
    git_workspace: The location in the shared volume to store git repos.
    out_dir: The location in the shared volume to store output artifacts.

  Returns:
    True if build succeeded or False on failure.
  """
  # TODO: Modify build_specified_commit function to return src dir.

  inferred_url, oss_fuzz_repo_name = build_specified_commit.detect_main_repo(
      project_name, repo_name=project_repo_name)
  src = utils.get_env_var(project_name, 'SRC')
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
    build_repo_manager.checkout_commit(commit_sha)
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
      'gcr.io/oss-fuzz/%s' % project_name,
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


def run_fuzzers(project_name, fuzz_seconds, out_dir):
  """Runs a all fuzzers for a specific OSS-Fuzz project.

  Args:
    project_name: The name of the OSS-Fuzz project being built.
    fuzz_seconds: The total time allotted for fuzzing.
    out_dir: The location in the shared volume to store output artifacts.

  Returns:
    (True if run was successful, True if error was found False if not).
  """
  fuzzer_paths = utils.get_fuzz_targets(out_dir)
  if not fuzzer_paths:
    logging.error('Error: No fuzzers were found in out directory.')
    return False, False

  fuzzer_timeout = int(fuzz_seconds / len(fuzzer_paths))
  fuzz_targets = [
      fuzz_target.FuzzTarget(project_name, fuzzer_path, fuzzer_timeout)
      for fuzzer_path in fuzzer_paths
  ]

  for target in fuzz_targets:
    test_case, stack_trace = target.fuzz()
    if not test_case or not stack_trace:
      logging.debug('Fuzzer %s, finished running.', target.target_name)
    else:
      logging.debug("Fuzzer %s, Detected Error: %s", target.target_name,
                    stack_trace)
      shutil.move(test_case, os.path.join(out_dir, 'testcase'))
      return True, True
  return True, False
