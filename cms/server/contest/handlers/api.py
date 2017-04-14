#!/usr/bin/env python2
# -*- coding: utf-8 -*-

# Contest Management System - http://cms-dev.github.io/
# Copyright © 2010-2013 Giovanni Mascellani <mascellani@poisson.phc.unipi.it>
# Copyright © 2010-2017 Stefano Maggiolo <s.maggiolo@gmail.com>
# Copyright © 2010-2012 Matteo Boscariol <boscarim@hotmail.com>
# Copyright © 2012-2014 Luca Wehrstedt <luca.wehrstedt@gmail.com>
# Copyright © 2014 Artem Iglikov <artem.iglikov@gmail.com>
# Copyright © 2014 Fabian Gundlach <320pointsguy@gmail.com>
# Copyright © 2016 Myungwoo Chun <mc.tamaki@gmail.com>
# TODO: add your name
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

"""Handlers for the API related to AWS

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

from urllib import quote

import tornado.web

from sqlalchemy import func

from cms import config
from cms.db import Task, UserTest, UserTestFile, UserTestManager, Testcase
from cms.grading.languagemanager import get_language
from cms.grading.tasktypes import get_task_type
from cms.server import actual_phase_required, format_size
from cmscommon.archive import Archive
from cmscommon.crypto import encrypt_number
from cmscommon.datetime import make_timestamp
from cmscommon.mimetypes import get_type_for_file_name

from .base import BaseHandler, FileHandler, \
    NOTIFICATION_ERROR, NOTIFICATION_SUCCESS


logger = logging.getLogger(__name__)


class APIGenerateOutput(BaseHandler):
    """Creates a user test on a task for a perticular testcase

        Based on UserTestHandler
    """
    def safe_get_item(self, cls, ident, session=None):
        """Get item from database of class cls and id ident, using
        session if given, or self.sql_session if not given. If id is
        not found, raise a 404.

        cls (type): class of object to retrieve.
        ident (string): id of object.
        session (Session|None): session to use.

        return (object): the object with the given id.

        raise (HTTPError): 404 if not found.

        """
        if session is None:
            session = self.sql_session
        entity = cls.get_from_id(ident, session)
        if entity is None:
            raise tornado.web.HTTPError(404)
        return entity

    @tornado.web.authenticated
    @actual_phase_required(0)
    def post(self, task_id, testcase_id):
        participation = self.current_user

        task = self.safe_get_item(Task, task_id)
        testcase = self.safe_get_item(Testcase, testcase_id)

        input_digest = testcase.input
        managers_names = [manager.filename for manager in task.active_dataset.managers.values()]

        # Check that the task is testable
        task_type = get_task_type(dataset=task.active_dataset)
        if not task_type.testable:
            raise tornado.web.HTTPError(403, "This task type is not testable")

        # Alias for easy access
        contest = self.contest

        # Required files from the user.
        required = set([sfe.filename for sfe in task.submission_format] +
                       task_type.get_user_managers(task.submission_format) +
                       ["input"])

        # TODO: Archive??

        # Ensure that the user did not submit multiple files with the
        # same name.
        if any(len(filename) != 1 for filename in self.request.files.values()):
            raise tornado.web.HTTPError(403, "Please select the correct files.")

        # This ensure that the user sent one file for every name in
        # submission format and no more.
        provided = set(list(self.request.files.keys()) + ["input"] + managers_names)
        if not (required == provided):
            raise tornado.web.HTTPError(403, "Please select the correct files.")

        # Add submitted files. After this, files is a dictionary indexed
        # by *our* filenames (something like "output01.txt" or
        # "taskname.%l", and whose value is a couple
        # (user_assigned_filename, content).
        files = {}
        for uploaded, data in self.request.files.iteritems():
            files[uploaded] = (data[0]["filename"], data[0]["body"])

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
                error = self._("Cannot recognize the user test language.")
            elif submission_lang not in contest.languages:
                error = self._("Language %s not allowed in this contest.") \
                        % submission_lang
            if error is not None:
                raise tornado.web.HTTPError(403, "%s" % error)

        # Check if submitted files are small enough.
        if any([len(f[1]) > config.max_submission_length
                for n, f in files.items() if n != "input"]):
            raise tornado.web.HTTPError(403,
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
                    participation.user.username)
                if not os.path.exists(path):
                    os.makedirs(path)
                # Pickle in ASCII format produces str, not unicode,
                # therefore we open the file in binary mode.
                with io.open(
                        os.path.join(path,
                                     "%d" % make_timestamp(self.timestamp)),
                        "wb") as file_:
                    pickle.dump((self.contest.id,
                                 participation.user.id,
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
                    "Test file %s sent by %s at %d." % (
                        filename, participation.user.username,
                        make_timestamp(self.timestamp)))
                file_digests[filename] = digest

            # Now Adding managers' digests
            for manager in task.active_dataset.managers.values():
                file_digests[manager.filename] = manager.digest

            # Finally Adding testcase's digest
            file_digests["input"] = input_digest

        # In case of error, the server aborts the submission
        except Exception as error:
            logger.error("Storage failed! %s", error)
            raise tornado.web.HTTPError(403, "Test storage failed!")

        # All the files are stored, ready to submit!
        logger.info("All files stored for test sent by %s",
                    participation.user.username)
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
        self.sql_session.commit()
        self.application.service.evaluation_service.new_user_test(
            user_test_id=user_test.id)
        # The argument (encripted user test id) is not used by CWS
        # (nor it discloses information to the user), but it is useful
        # for automatic testing to obtain the user test id).
        self.write('%d' % user_test.id)


class APISumbitionDetails(BaseHandler):
    """Gets the result of the sumbission

        Based on UserTestDetailsHandler
    """
    def safe_get_item(self, cls, ident, session=None):
        """Get item from database of class cls and id ident, using
        session if given, or self.sql_session if not given. If id is
        not found, raise a 404.

        cls (type): class of object to retrieve.
        ident (string): id of object.
        session (Session|None): session to use.

        return (object): the object with the given id.

        raise (HTTPError): 404 if not found.

        """
        if session is None:
            session = self.sql_session
        entity = cls.get_from_id(ident, session)
        if entity is None:
            raise tornado.web.HTTPError(404)
        return entity

    @tornado.web.authenticated
    @actual_phase_required(0)
    def get(self, task_id, user_test_num):
        participation = self.current_user

        if not self.r_params["testing_enabled"]:
            raise tornado.web.HTTPError(404)

        task = self.safe_get_item(Task, task_id)

        user_test = self.sql_session.query(UserTest) \
            .filter(UserTest.participation == participation) \
            .filter(UserTest.task == task) \
            .order_by(UserTest.timestamp) \
            .offset(int(user_test_num) - 1) \
            .first()
        if user_test is None:
            raise tornado.web.HTTPError(404)

        tr = user_test.get_result(task.active_dataset)
        if tr is None:
            raise tornado.web.HTTPError(404)

        result = {}

        if tr is not None and tr.evaluated():
            result['evalres'] = tr.evaluation_text
        else:
            result['evalres'] = 'none'
            self.write(json.dumps(result))
            return

        if tr is not None and tr.compiled():
            result['compiled'] = tr.compilation_text
        else:
            result['compiled'] = 'none'
            self.write(json.dumps(result))
            return

        if tr.compilation_time is None:
            result['time'] = 'none'
        else:
            result['time'] = tr.compilation_time

        if tr.compilation_memory is None:
            result['memory'] = 'none'
        else:
            result['memory'] = tr.compilation_memory

        self.write(json.dumps(result))
