# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

from typing import Any

import sqlalchemy as sa
from flask_appbuilder import Model
from sqlalchemy.orm import backref, relationship
from sqlalchemy.types import Text

from superset.constants import PASSWORD_MASK
from superset.extensions import encrypted_field_factory
from superset.models.core import Database
from superset.models.helpers import (
    AuditMixinNullable,
    ExtraJSONMixin,
    ImportExportMixin,
)


class SSHTunnel(AuditMixinNullable, ExtraJSONMixin, ImportExportMixin, Model):
    """
    A ssh tunnel configuration in a database.
    """

    __tablename__ = "ssh_tunnels"

    id = sa.Column(sa.Integer, primary_key=True)
    database_id = sa.Column(
        sa.Integer, sa.ForeignKey("dbs.id"), nullable=False, unique=True
    )
    database: Database = relationship(
        "Database",
        backref=backref("ssh_tunnels", uselist=False, cascade="all, delete-orphan"),
        foreign_keys=[database_id],
    )

    server_address = sa.Column(sa.Text)
    server_port = sa.Column(sa.Integer)
    username = sa.Column(encrypted_field_factory.create(Text))

    # basic authentication
    password = sa.Column(encrypted_field_factory.create(Text), nullable=True)

    # password protected pkey authentication
    private_key = sa.Column(encrypted_field_factory.create(Text), nullable=True)
    private_key_password = sa.Column(
        encrypted_field_factory.create(Text), nullable=True
    )

    export_fields = [
        "server_address",
        "server_port",
        "username",
        "password",
        "private_key",
        "private_key_password",
    ]

    extra_import_fields = [
        "database_id",
    ]

    @property
    def data(self) -> dict[str, Any]:
        output = {
            "id": self.id,
            "server_address": self.server_address,
            "server_port": self.server_port,
            "username": self.username,
        }
        if self.password is not None:
            output["password"] = PASSWORD_MASK
        if self.private_key is not None:
            output["private_key"] = PASSWORD_MASK
        if self.private_key_password is not None:
            output["private_key_password"] = PASSWORD_MASK
        return output
