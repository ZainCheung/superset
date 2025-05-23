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
"""Country map use lowercase country name

Revision ID: 085f06488938
Revises: 134cea61c5e7
Create Date: 2021-04-09 16:14:19.040884

"""

from alembic import op
from sqlalchemy import Column, Integer, String, Text
from sqlalchemy.ext.declarative import declarative_base

from superset import db
from superset.utils import json

# revision identifiers, used by Alembic.
revision = "085f06488938"
down_revision = "134cea61c5e7"

Base = declarative_base()


class Slice(Base):
    __tablename__ = "slices"

    id = Column(Integer, primary_key=True)
    params = Column(Text)
    viz_type = Column(String(250))


def upgrade():
    """
    Convert all country names to lowercase
    """
    bind = op.get_bind()
    session = db.Session(bind=bind)

    for slc in session.query(Slice).filter(Slice.viz_type == "country_map").all():
        try:
            params = json.loads(slc.params)
            if params.get("select_country"):
                params["select_country"] = params["select_country"].lower()
                slc.params = json.dumps(params, sort_keys=True)
        except Exception:  # noqa: S110
            pass

    session.commit()
    session.close()


def downgrade():
    """
    Convert all country names to sentence case
    """
    bind = op.get_bind()
    session = db.Session(bind=bind)

    for slc in session.query(Slice).filter(Slice.viz_type == "country_map").all():
        try:
            params = json.loads(slc.params)
            if params.get("select_country"):
                country = params["select_country"].lower()
                params["select_country"] = country[0].upper() + country[1:]
                slc.params = json.dumps(params, sort_keys=True)
        except Exception:  # noqa: S110
            pass

    session.commit()
    session.close()
