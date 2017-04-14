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

import json
import logging
import traceback

import tornado.web

from cms.db import Attachment, Dataset, Session, Statement, Submission, \
    SubmissionFormatElement, Task, Testcase
from cmscommon.datetime import make_datetime
from cms import plugin_list
from cms.grading.languagemanager import LANGUAGES

from .base import BaseHandler, SimpleHandler, require_permission


logger = logging.getLogger(__name__)


class APITaskTypesHandler(BaseHandler):
    """Writes a list of task types names

    """

    @require_permission(BaseHandler.PERMISSION_ALL)
    def get(self):
        task_type_list = plugin_list("cms.grading.tasktypes", "tasktypes")
        task_types_name = [task_type.__name__ for task_type in task_type_list]
        return self.write(json.dumps(task_types_name))


class APIScoreTypesHandler(BaseHandler):
    """Writes a list of task types names

    """

    @require_permission(BaseHandler.PERMISSION_ALL)
    def get(self):
        score_type_list = plugin_list("cms.grading.scoretypes", "scoretypes")
        score_types_name = [score_type.__name__ for score_type in score_type_list]
        return self.write(json.dumps(score_types_name))


class APILanguages(BaseHandler):
    """Writes a list of language names

    """

    @require_permission(BaseHandler.PERMISSION_ALL)
    def get(self):
        language_names = [lang.name for lang in LANGUAGES]
        return self.write(json.dumps(language_names))


class APIAddTask(BaseHandler):
    """Creates a new task.

        Based on AddTaskHandler
    """

    @require_permission(BaseHandler.PERMISSION_ALL)
    def post(self):
        #
        # TODO: helpers and managers
        # TODO: add to a contest
        #
        try:
            attrs = dict()

            self.get_string(attrs, "name", empty=None)
            assert attrs.get("name") is not None, "No task name specified."
            attrs["title"] = attrs["name"]

            # Set default submission format as ["taskname.%l"]
            attrs["submission_format"] = \
                [SubmissionFormatElement("%s.%%l" % attrs["name"])]

            # Create the task.
            task = Task(**attrs)
            self.sql_session.add(task)
        except Exception as error:
            raise tornado.web.HTTPError(403, "Invalid fields: %s" % error)

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
            raise tornado.web.HTTPError(403, "Invalid fields: %s" % error)

        if self.try_commit():
            # Create the task on RWS.
            self.application.service.proxy_service.reinitialize()
            self.write('%d' % task.id)
        else:
            raise tornado.web.HTTPError(403, "Operation Unsuccessful!")


class APIModifyTask(BaseHandler):
    """Updates an existing task.

        Based on TaskHandler
    """

    @require_permission(BaseHandler.PERMISSION_ALL)
    def post(self, task_id):
        #
        # TODO: helpers and managers
        #
        task = self.safe_get_item(Task, task_id)

        try:
            attrs = task.get_attrs()

            self.get_string(attrs, "name", empty=None)
            assert attrs.get("name") is not None, "No task name specified."
            attrs["title"] = attrs["name"]

            # Set default submission format as ["taskname.%l"]
            attrs["submission_format"] = \
                [SubmissionFormatElement("%s.%%l" % attrs["name"])]

            # Update the task.
            task.set_attrs(attrs)

        except Exception as error:
            raise tornado.web.HTTPError(403, "Invalid fields: %s" % error)

        try:
            dataset = task.active_dataset
            attrs = dataset.get_attrs()

            # Create its first dataset.
            self.get_time_limit(attrs, "time_limit")
            self.get_memory_limit(attrs, "memory_limit")
            self.get_task_type(attrs, "task_type", "task_type_parameters_")
            self.get_score_type(attrs, "score_type", "score_type_parameters")

            # Update the dataset.
            dataset.set_attrs(attrs)

        except Exception as error:
            raise tornado.web.HTTPError(403, "Invalid fields: %s" % error)

        if self.try_commit():
            # Update the task and score on RWS.
            self.application.service.proxy_service.dataset_updated(
                task_id=task.id)
            self.write('%d' % task.id)
        else:
            raise tornado.web.HTTPError(403, "Operation Unsuccessful!")


class APIAddTestcase(BaseHandler):
    """Add a testcase to the task's active dataset.

        Based on AddTestcaseHandler
    """

    @require_permission(BaseHandler.PERMISSION_ALL)
    def post(self, task_id):
        task = self.safe_get_item(Task, task_id)
        dataset = task.active_dataset

        codename = self.get_argument("testcase_id")

        try:
            input_ = self.request.files["input"][0]
            output = self.request.files["output"][0]
        except KeyError:
            raise tornado.web.HTTPError(403, "Invalid data: Please fill both input and output.")

        public = True
        task_name = task.name
        self.sql_session.close()

        try:
            input_digest = \
                self.application.service.file_cacher.put_file_content(
                    input_["body"],
                    "Testcase input for task %s" % task_name)
            output_digest = \
                self.application.service.file_cacher.put_file_content(
                    output["body"],
                    "Testcase output for task %s" % task_name)
        except Exception as error:
            raise tornado.web.HTTPError(403, "Testcase storage failed: %s" % error)

        self.sql_session = Session()

        testcase = Testcase(
            codename, public, input_digest, output_digest, dataset=dataset)
        self.sql_session.add(testcase)

        if self.try_commit():
            # max_score and/or extra_headers might have changed.
            self.application.service.proxy_service.reinitialize()
            self.write('%d' % testcase.id)
        else:
            raise tornado.web.HTTPError(403, "Operation Unsuccessful!")
