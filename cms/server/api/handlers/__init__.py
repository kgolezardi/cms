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

from __future__ import absolute_import
from __future__ import print_function
from __future__ import unicode_literals

from .handler import \
    TestHandler, \
    TaskTypesHandler, \
    ScoreTypesHandler, \
    LanguagesHandler, \
    AddTaskHandler, \
    ModifyTaskHandler, \
    AddTestcaseHandler, \
    GenerateOutputHandler, \
    SubmissionDetailsHandler

HANDLERS = [
    (r"/", TestHandler),
    (r"/tasktypes", TaskTypesHandler),
    (r"/scoretypes", ScoreTypesHandler),
    (r"/languages", LanguagesHandler),
    (r"/tasks/add", AddTaskHandler),

    (r"/task/([0-9]+)/modify", ModifyTaskHandler),
    (r"/task/([0-9]+)/testcases/add", AddTestcaseHandler),

    (r"/task/([0-9]+)/testcase/([0-9]+)/run", GenerateOutputHandler),
    (r"/task/([0-9]+)/test/([0-9]+)/result", SubmissionDetailsHandler),
]


__all__ = ["HANDLERS"]
