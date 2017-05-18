#!/usr/bin/env python2
# -*- coding: utf-8 -*-

# Contest Management System - http://cms-dev.github.io/
# Copyright © 2017 Kiarash Golezardi <kiarashgolezardi@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Handlers for the API

"""
from __future__ import absolute_import
from __future__ import print_function
from __future__ import unicode_literals

import io
import logging
import os
import pickle
import re
import json
import base64

import tornado.web

from cms import config
from cms.db import Attachment, Dataset, Session, Manager, Submission, \
    SubmissionFormatElement, Task, UserTest, UserTestFile, \
    UserTestManager, Testcase, Participation
from cms.grading.tasktypes import get_task_type
from cms.grading.languagemanager import get_language
from cmscommon.datetime import make_datetime, make_timestamp
from cms import plugin_list
from cms.grading.languagemanager import LANGUAGES

from .base import BaseHandler, FileHandler


logger = logging.getLogger(__name__)


class TestHandler(BaseHandler):

    def get(self):
        self.r_params = self.render_params()
        self.render("test.html", **self.r_params)

    def post(self):
        input_file = self.request.files["input"][0]
        operation = self.get_argument("operation", "encode")
        if operation == "encode":
            encoded = base64.b64encode(input_file["body"])
            self.write('%s' % encoded)
        else:
            self.write('%s' % input_file)


class TaskTypesHandler(BaseHandler):
    """Writes a list of task types names

    """

    def get(self):
        task_type_list = plugin_list("cms.grading.tasktypes", "tasktypes")
        task_types_name = [task_type.__name__ for task_type in task_type_list]
        return self.write(json.dumps(task_types_name))


class ScoreTypesHandler(BaseHandler):
    """Writes a list of task types names

    """

    def get(self):
        score_type_list = plugin_list("cms.grading.scoretypes", "scoretypes")
        score_types_name = [score_type.__name__ for score_type in score_type_list]
        return self.write(json.dumps(score_types_name))


class LanguagesHandler(BaseHandler):
    """Writes a list of language names

    """

    def get(self):
        language_names = [lang.name for lang in LANGUAGES]
        return self.write(json.dumps(language_names))


class AddTaskHandler(BaseHandler):
    """Creates a new task.

        Based on AWS AddTaskHandler
    """

    def post(self):
        try:
            attrs = dict()

            self.get_string(attrs, "name", empty=None)
            assert attrs.get("name") is not None, "No task name specified."
            attrs["title"] = attrs["name"]

            self.get_submission_format(attrs)

            # Create the task.
            task = Task(**attrs)
            self.sql_session.add(task)
        except Exception as error:
            return self.APIOutput(False, "Invalid fields: %s" % error)

        try:
            attrs = dict()

            # Create its first dataset.
            attrs["description"] = "Default"
            attrs["autojudge"] = True
            self.get_time_limit(attrs, "time_limit")
            self.get_memory_limit(attrs, "memory_limit")
            self.get_task_type(attrs, "task_type", "task_type_parameters_")
            self.get_score_type(attrs, "score_type", "score_type_parameters")
            attrs["task"] = task
            dataset = Dataset(**attrs)
            self.sql_session.add(dataset)

            # Make the dataset active. Life works better that way.
            task.active_dataset = dataset

        except Exception as error:
            return self.APIOutput(False, "Invalid fields: %s" % error)

        managers = json.loads(str(self.get_argument("managers")))
        for filename in managers:
            try:
                body = base64.b64decode(managers[filename])
            except TypeError:
                return self.APIOutput(False, "Invalid data: Please provide a base64 encoded file")

            try:
                digest = self.application.service.file_cacher.put_file_content(
                    body,
                    "Task manager for %s" % attrs["name"])
            except Exception as error:
                return self.APIOutput(False, "Manager storage failed: %s" % error)

            manager = Manager(filename, digest, dataset=dataset)
            self.sql_session.add(manager)

        if self.try_commit():
            return self.APIOutput(True, '%d' % task.id)
        else:
            return self.APIOutput(False, "Operation Unsuccessful!")


class RemoveTaskHandler(BaseHandler):
    """Updates an existing task.

        Based on AWS RemoveTaskHandler
    """

    def get(self, task_name):
        task = self.get_task_by_name(task_name)

        self.sql_session.delete(task)
        if self.try_commit():
            return self.APIOutput(True, 'Successful')
        else:
            return self.APIOutput(False, 'Unsuccessful')


class AddTestcaseHandler(BaseHandler):
    """Add a testcase to the task's active dataset.

        Based on AWS AddTestcaseHandler
    """

    def post(self, task_name):
        task = self.get_task_by_name(task_name)
        dataset = task.active_dataset

        codename = self.get_argument("testcase_id")

        try:
            input_base64 = str(self.get_argument("input"))
            input_body = str(base64.b64decode(input_base64))
            output_base64 = str(self.get_argument("output"))
            output_body = str(base64.b64decode(output_base64))

        except TypeError:
            return self.APIOutput(False, "Invalid data: Please give a valid input")

        public = True
        task_name = task.name
        self.sql_session.close()

        try:
            input_digest = \
                self.application.service.file_cacher.put_file_content(
                    input_body,
                    "Testcase input for task %s" % task_name)
            output_digest = \
                self.application.service.file_cacher.put_file_content(
                    output_body,
                    "Testcase output for task %s" % task_name)
        except Exception as error:
            return self.APIOutput(False, "Testcase storage failed: %s" % error)

        self.sql_session = Session()

        testcase = Testcase(
            codename, public, input_digest, output_digest, dataset=dataset)
        self.sql_session.add(testcase)

        if self.try_commit():
            return self.APIOutput(True, '%d' % testcase.id)
        else:
            return self.APIOutput(False, "Operation Unsuccessful!")


class GenerateOutputHandler(BaseHandler):
    """Creates a user test on a task for a perticular testcase

        Based on CWS UserTestHandler
    """

    def post(self, task_name, testcase_id):
        # TODO: Create a special contest, user, and participation instead of
        # using the first one you see
        participation = self.sql_session.query(Participation).first()

        task = self.get_task_by_name(task_name)
        testcase = self.safe_get_item(Testcase, testcase_id)

        request_files = json.loads(str(self.get_argument("files")))

        input_digest = testcase.input

        # Check that the task is testable
        task_type = get_task_type(dataset=task.active_dataset)
        if not task_type.testable:
            return self.APIOutput(False, "This task type is not testable")

        # Required files from the user.
        required = set([sfe.filename for sfe in task.submission_format] +
                       task_type.get_user_managers(task.submission_format) +
                       ["input"])

        # TODO: If it is necessary, we may have to extract archives

        # This ensure that the user sent one file for every name in
        # submission format and no more.
        provided = set(list(request_files.keys()) + ["input"])
        if not (required == provided):
            return self.APIOutput(False, "Please send the correct files.")

        # Add submitted files. After this, files is a dictionary indexed
        # by *our* filenames (something like "output01.txt" or
        # "taskname.%l", and whose value is a couple
        # (our_filename, content)
        try:
            files = {}
            for filename, body in request_files.iteritems():
                files[filename] = (filename, base64.b64decode(body))
        except TypeError:
            return self.APIOutput(False, "Invalid data: Please provide a base64 encoded file")

        # Read the submission language provided in the request; we
        # integrate it with the language fetched from the previous
        # submission (if we use it) and later make sure it is
        # recognized and allowed.
        submission_lang = self.get_argument("language", None)
        need_lang = any(our_filename.find(".%l") != -1
                        for our_filename in files)

        # Throw an error if task needs a language, but we don't have
        # it or it is not allowed / recognized.
        if need_lang:
            error = None
            if submission_lang is None:
                error = "Cannot recognize the user test language."
            if error is not None:
                return self.APIOutput(False, "%s" % error)

        # Check if submitted files are small enough.
        if any([len(f[1]) > config.max_submission_length
                for n, f in files.items() if n != "input"]):
            return self.APIOutput(False,
                                  "Each source file must be at most %d bytes long."
                                   % config.max_submission_length)

        # All checks done, submission accepted.

        # Attempt to store the submission locally to be able to
        # recover a failure.

        if config.tests_local_copy:
            try:
                path = os.path.join(
                    config.tests_local_copy_path.replace("%s",
                                                         config.data_dir),
                    'API')
                if not os.path.exists(path):
                    os.makedirs(path)
                # Pickle in ASCII format produces str, not unicode,
                # therefore we open the file in binary mode.
                with io.open(
                        os.path.join(path,
                                     "%d" % make_timestamp(self.timestamp)),
                        "wb") as file_:
                    pickle.dump((None,
                                 None,
                                 task.id,
                                 files), file_)
            except Exception as error:
                logger.error("Test local copy failed.", exc_info=True)

        # We now have to send all the files to the destination...
        file_digests = {}
        try:
            for filename in files:
                digest = self.application.service.file_cacher.put_file_content(
                    files[filename][1],
                    "Test file %s sent by API at %d." % (
                        filename, make_timestamp(self.timestamp)))
                file_digests[filename] = digest

            # Now Adding managers' digests
            for manager in task.active_dataset.managers.values():
                file_digests[manager.filename] = manager.digest

            # Finally Adding testcase's digest
            file_digests["input"] = input_digest

        # In case of error, the server aborts the submission
        except Exception as error:
            logger.error("Storage failed! %s", error)
            return self.APIOutput(False, "Test storage failed!")

        # All the files are stored, ready to submit!
        logger.info("All files stored for test sent by API")
        user_test = UserTest(self.timestamp,
                             submission_lang,
                             file_digests["input"],
                             participation=participation,
                             task=task)

        for filename in [sfe.filename for sfe in task.submission_format]:
            digest = file_digests[filename]
            self.sql_session.add(
                UserTestFile(filename, digest, user_test=user_test))
        for filename in task_type.get_user_managers(task.submission_format):
            digest = file_digests[filename]
            if submission_lang is not None:
                extension = get_language(submission_lang).source_extension
                filename = filename.replace(".%l", extension)
            self.sql_session.add(
                UserTestManager(filename, digest, user_test=user_test))

        self.sql_session.add(user_test)
        try:
            self.sql_session.commit()
        except Exception as error:
            return self.APIOutput(False, '%s' % error)
        self.application.service.evaluation_service.new_user_test(
            user_test_id=user_test.id)
        return self.APIOutput(True, '%d' % user_test.id)


class SubmissionDetailsHandler(BaseHandler):
    """Gets the result of the submission

        Based on CWS UserTestDetailsHandler
    """

    def get(self, task_name, user_test_num):
        task = self.get_task_by_name(task_name)
        user_test = self.safe_get_item(UserTest, user_test_num)

        if user_test is None:
            return self.APIOutput(False, '')

        tr = user_test.get_result(task.active_dataset)
        if tr is None:
            return self.APIOutput(False, '')

        result = {}

        if tr is not None and tr.evaluated():
            result['evalres'] = tr.evaluation_text
        else:
            result['evalres'] = 'none'
            return self.APIOutput(True, json.dumps(result))

        if tr is not None and tr.compiled():
            result['compiled'] = tr.compilation_text
        else:
            result['compiled'] = 'none'
            return self.APIOutput(True, json.dumps(result))

        if tr.compilation_time is None:
            result['time'] = 'none'
        else:
            result['time'] = tr.compilation_time

        if tr.compilation_memory is None:
            result['memory'] = 'none'
        else:
            result['memory'] = tr.compilation_memory

        return self.APIOutput(True, json.dumps(result))


class SubmissionOutputHandler(FileHandler):
    """Send back a submission output.

        Based on CWS UserTestIOHandler
    """
    def get(self, task_name, test_id):
        task = self.get_task_by_name(task_name)
        user_test = self.safe_get_item(UserTest, test_id)

        if user_test is None:
            raise tornado.web.HTTPError(404)

        tr = user_test.get_result(task.active_dataset)
        digest = tr.output if tr is not None else None
        self.sql_session.close()

        if digest is None:
            raise tornado.web.HTTPError(404)

        mimetype = 'text/plain'

        self.fetch(digest, mimetype, 'output')
        # TODO: Probably best not send the file in this way, but to
        # write the content, as a base64 encoded string.
