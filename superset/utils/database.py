#  Licensed to the Apache Software Foundation (ASF) under one
#  or more contributor license agreements.  See the NOTICE file
#  distributed with this work for additional information
#  regarding copyright ownership.  The ASF licenses this file
#  to you under the Apache License, Version 2.0 (the
#  "License"); you may not use this file except in compliance
#  with the License.  You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing,
#  software distributed under the License is distributed on an
#  "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
#  KIND, either express or implied.  See the License for the
#  specific language governing permissions and limitations
#  under the License.
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from flask import current_app as app

from superset.constants import EXAMPLES_DB_UUID

if TYPE_CHECKING:
    from superset.connectors.sqla.models import Database

logging.getLogger("MARKDOWN").setLevel(logging.INFO)
logger = logging.getLogger(__name__)


# TODO: duplicate code with DatabaseDao, below function should be moved or use dao
def get_or_create_db(
    database_name: str, sqlalchemy_uri: str, always_create: bool | None = True
) -> Database:
    # pylint: disable=import-outside-toplevel
    from superset import db
    from superset.models import core as models

    database = (
        db.session.query(models.Database).filter_by(database_name=database_name).first()
    )

    # databases with a fixed UUID
    uuids = {
        "examples": EXAMPLES_DB_UUID,
    }

    if not database and always_create:
        logger.info("Creating database reference for %s", database_name)
        database = models.Database(
            database_name=database_name, uuid=uuids.get(database_name)
        )
        db.session.add(database)
        database.set_sqlalchemy_uri(sqlalchemy_uri)

    # todo: it's a bad idea to do an update in a get/create function
    if database and database.sqlalchemy_uri_decrypted != sqlalchemy_uri:
        database.set_sqlalchemy_uri(sqlalchemy_uri)

    db.session.flush()
    return database


def get_example_database() -> Database:
    # pylint: disable=import-outside-toplevel

    return get_or_create_db("examples", app.config["SQLALCHEMY_EXAMPLES_URI"])


def get_main_database() -> Database:
    # pylint: disable=import-outside-toplevel

    db_uri = app.config["SQLALCHEMY_DATABASE_URI"]
    return get_or_create_db("main", db_uri)


# TODO - the below method used by tests so should move there but should move together
# with above function... think of how to refactor it
def remove_database(database: Database) -> None:
    # pylint: disable=import-outside-toplevel
    from superset import db

    db.session.delete(database)
    db.session.flush()
