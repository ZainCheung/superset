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
# pylint: disable=too-many-lines

from __future__ import annotations

import logging
import re
import warnings
from datetime import datetime
from inspect import signature
from re import Match, Pattern
from typing import (
    Any,
    Callable,
    cast,
    ContextManager,
    NamedTuple,
    TYPE_CHECKING,
    TypedDict,
    Union,
)
from urllib.parse import urlencode, urljoin
from uuid import uuid4

import pandas as pd
import requests
from apispec import APISpec
from apispec.ext.marshmallow import MarshmallowPlugin
from deprecation import deprecated
from flask import current_app as app, g, url_for
from flask_appbuilder.security.sqla.models import User
from flask_babel import gettext as __, lazy_gettext as _
from marshmallow import fields, Schema
from marshmallow.validate import Range
from sqlalchemy import column, select, types
from sqlalchemy.engine.base import Engine
from sqlalchemy.engine.interfaces import Compiled, Dialect
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql import literal_column, quoted_name, text
from sqlalchemy.sql.expression import BinaryExpression, ColumnClause, Select, TextClause
from sqlalchemy.types import TypeEngine

from superset import db
from superset.constants import QUERY_CANCEL_KEY, TimeGrain as TimeGrainConstants
from superset.databases.utils import get_table_metadata, make_url_safe
from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
from superset.exceptions import OAuth2Error, OAuth2RedirectError
from superset.sql.parse import (
    BaseSQLStatement,
    LimitMethod,
    RLSMethod,
    SQLScript,
    SQLStatement,
    Table,
)
from superset.superset_typing import (
    OAuth2ClientConfig,
    OAuth2State,
    OAuth2TokenResponse,
    ResultSetColumnType,
    SQLAColumnType,
)
from superset.utils import core as utils, json
from superset.utils.core import ColumnSpec, GenericDataType, QuerySource
from superset.utils.hashing import md5_sha_from_str
from superset.utils.json import redact_sensitive, reveal_sensitive
from superset.utils.network import is_hostname_valid, is_port_open
from superset.utils.oauth2 import encode_oauth2_state

if TYPE_CHECKING:
    from superset.connectors.sqla.models import TableColumn
    from superset.databases.schemas import TableMetadataResponse
    from superset.models.core import Database
    from superset.models.sql_lab import Query


ColumnTypeMapping = tuple[
    Pattern[str],
    Union[TypeEngine, Callable[[Match[str]], TypeEngine]],
    GenericDataType,
]

logger = logging.getLogger()

# When connecting to a database it's hard to catch specific exceptions, since we support
# more than 50 different database drivers. Usually the try/except block will catch the
# generic `Exception` class, which requires a pylint disablee comment. To make it clear
# that we know this is a necessary evil we create an alias, and catch it instead.
GenericDBException = Exception


def convert_inspector_columns(cols: list[SQLAColumnType]) -> list[ResultSetColumnType]:
    result_set_columns: list[ResultSetColumnType] = []
    for col in cols:
        result_set_columns.append({"column_name": col.get("name"), **col})  # type: ignore
    return result_set_columns


class TimeGrain(NamedTuple):
    name: str  # TODO: redundant field, remove
    label: str
    function: str
    duration: str | None


builtin_time_grains: dict[str | None, str] = {
    TimeGrainConstants.SECOND: _("Second"),
    TimeGrainConstants.FIVE_SECONDS: _("5 second"),
    TimeGrainConstants.THIRTY_SECONDS: _("30 second"),
    TimeGrainConstants.MINUTE: _("Minute"),
    TimeGrainConstants.FIVE_MINUTES: _("5 minute"),
    TimeGrainConstants.TEN_MINUTES: _("10 minute"),
    TimeGrainConstants.FIFTEEN_MINUTES: _("15 minute"),
    TimeGrainConstants.THIRTY_MINUTES: _("30 minute"),
    TimeGrainConstants.HOUR: _("Hour"),
    TimeGrainConstants.SIX_HOURS: _("6 hour"),
    TimeGrainConstants.DAY: _("Day"),
    TimeGrainConstants.WEEK: _("Week"),
    TimeGrainConstants.MONTH: _("Month"),
    TimeGrainConstants.QUARTER: _("Quarter"),
    TimeGrainConstants.YEAR: _("Year"),
    TimeGrainConstants.WEEK_STARTING_SUNDAY: _("Week starting Sunday"),
    TimeGrainConstants.WEEK_STARTING_MONDAY: _("Week starting Monday"),
    TimeGrainConstants.WEEK_ENDING_SATURDAY: _("Week ending Saturday"),
    TimeGrainConstants.WEEK_ENDING_SUNDAY: _("Week ending Sunday"),
}


class TimestampExpression(ColumnClause):  # pylint: disable=abstract-method, too-many-ancestors
    def __init__(self, expr: str, col: ColumnClause, **kwargs: Any) -> None:
        """Sqlalchemy class that can be used to render native column elements respecting
        engine-specific quoting rules as part of a string-based expression.

        :param expr: Sql expression with '{col}' denoting the locations where the col
        object will be rendered.
        :param col: the target column
        """
        super().__init__(expr, **kwargs)
        self.col = col

    @property
    def _constructor(self) -> ColumnClause:
        # Needed to ensure that the column label is rendered correctly when
        # proxied to the outer query.
        # See https://github.com/sqlalchemy/sqlalchemy/issues/4730
        return ColumnClause


@compiles(TimestampExpression)
def compile_timegrain_expression(
    element: TimestampExpression, compiler: Compiled, **kwargs: Any
) -> str:
    return element.name.replace("{col}", compiler.process(element.col, **kwargs))


class MetricType(TypedDict, total=False):
    """
    Type for metrics return by `get_metrics`.
    """

    metric_name: str
    expression: str
    verbose_name: str | None
    metric_type: str | None
    description: str | None
    d3format: str | None
    currency: str | None
    warning_text: str | None
    extra: str | None


class BaseEngineSpec:  # pylint: disable=too-many-public-methods
    """Abstract class for database engine specific configurations

    Attributes:
        allows_alias_to_source_column: Whether the engine is able to pick the
                                       source column for aggregation clauses
                                       used in ORDER BY when a column in SELECT
                                       has an alias that is the same as a source
                                       column.
        allows_hidden_orderby_agg:     Whether the engine allows ORDER BY to
                                       directly use aggregation clauses, without
                                       having to add the same aggregation in SELECT.
    """

    engine_name: str | None = None  # for user messages, overridden in child classes

    # These attributes map the DB engine spec to one or more SQLAlchemy dialects/drivers;  # noqa: E501
    # see the ``supports_url`` and ``supports_backend`` methods below.
    engine = "base"  # str as defined in sqlalchemy.engine.engine
    engine_aliases: set[str] = set()
    drivers: dict[str, str] = {}
    default_driver: str | None = None

    # placeholder with the SQLAlchemy URI template
    sqlalchemy_uri_placeholder = (
        "engine+driver://user:password@host:port/dbname[?key=value&key=value...]"
    )

    disable_ssh_tunneling = False

    _date_trunc_functions: dict[str, str] = {}
    _time_grain_expressions: dict[str | None, str] = {}
    _default_column_type_mappings: tuple[ColumnTypeMapping, ...] = (
        (
            re.compile(r"^string", re.IGNORECASE),
            types.String(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r"^n((var)?char|text)", re.IGNORECASE),
            types.UnicodeText(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r"^(var)?char", re.IGNORECASE),
            types.String(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r"^(tiny|medium|long)?text", re.IGNORECASE),
            types.String(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r"^smallint", re.IGNORECASE),
            types.SmallInteger(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^int(eger)?", re.IGNORECASE),
            types.Integer(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^bigint", re.IGNORECASE),
            types.BigInteger(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^long", re.IGNORECASE),
            types.Float(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^decimal", re.IGNORECASE),
            types.Numeric(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^numeric", re.IGNORECASE),
            types.Numeric(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^float", re.IGNORECASE),
            types.Float(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^double", re.IGNORECASE),
            types.Float(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^real", re.IGNORECASE),
            types.REAL,
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^smallserial", re.IGNORECASE),
            types.SmallInteger(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^serial", re.IGNORECASE),
            types.Integer(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^bigserial", re.IGNORECASE),
            types.BigInteger(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^money", re.IGNORECASE),
            types.Numeric(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^timestamp", re.IGNORECASE),
            types.TIMESTAMP(),
            GenericDataType.TEMPORAL,
        ),
        (
            re.compile(r"^datetime", re.IGNORECASE),
            types.DateTime(),
            GenericDataType.TEMPORAL,
        ),
        (
            re.compile(r"^date", re.IGNORECASE),
            types.Date(),
            GenericDataType.TEMPORAL,
        ),
        (
            re.compile(r"^time", re.IGNORECASE),
            types.Time(),
            GenericDataType.TEMPORAL,
        ),
        (
            re.compile(r"^interval", re.IGNORECASE),
            types.Interval(),
            GenericDataType.TEMPORAL,
        ),
        (
            re.compile(r"^bool(ean)?", re.IGNORECASE),
            types.Boolean(),
            GenericDataType.BOOLEAN,
        ),
    )
    # engine-specific type mappings to check prior to the defaults
    column_type_mappings: tuple[ColumnTypeMapping, ...] = ()

    # type-specific functions to mutate values received from the database.
    # Needed on certain databases that return values in an unexpected format
    column_type_mutators: dict[TypeEngine, Callable[[Any], Any]] = {}

    # Does database support join-free timeslot grouping
    time_groupby_inline = False
    limit_method = LimitMethod.FORCE_LIMIT
    supports_multivalues_insert = False
    allows_joins = True
    allows_subqueries = True
    allows_alias_in_select = True
    allows_alias_in_orderby = True
    allows_sql_comments = True
    allows_escaped_colons = True

    # Whether ORDER BY clause can use aliases created in SELECT
    # that are the same as a source column
    allows_alias_to_source_column = True

    # Whether ORDER BY clause must appear in SELECT
    # if True, then it doesn't have to.
    allows_hidden_orderby_agg = True

    # Whether ORDER BY clause can use sql calculated expression
    # if True, use alias of select column for `order by`
    # the True is safely for most database
    # But for backward compatibility, False by default
    allows_hidden_cc_in_orderby = False

    # Whether allow CTE as subquery or regular CTE
    # If True, then it will allow  in subquery ,
    # if False it will allow as regular CTE
    allows_cte_in_subquery = True
    # Define alias for CTE
    cte_alias = "__cte"
    # A set of disallowed connection query parameters by driver name
    disallow_uri_query_params: dict[str, set[str]] = {}
    # A Dict of query parameters that will always be used on every connection
    # by driver name

    # Whether to use equality operators (= true/false) instead of IS operators
    # for boolean filters. Some databases like Snowflake don't support IS true/false
    use_equality_for_boolean_filters = False
    enforce_uri_query_params: dict[str, dict[str, Any]] = {}

    force_column_alias_quotes = False
    arraysize = 0
    max_column_name_length: int | None = None
    try_remove_schema_from_table_name = True  # pylint: disable=invalid-name
    run_multiple_statements_as_one = False
    custom_errors: dict[
        Pattern[str], tuple[str, SupersetErrorType, dict[str, Any]]
    ] = {}

    # List of JSON path to fields in `encrypted_extra` that should be masked when the
    # database is edited. By default everything is masked.
    # pylint: disable=invalid-name
    encrypted_extra_sensitive_fields: set[str] = {"$.*"}

    # Whether the engine supports file uploads
    # if True, database will be listed as option in the upload file form
    supports_file_upload = True

    # Is the DB engine spec able to change the default schema? This requires implementing  # noqa: E501
    # a custom `adjust_engine_params` method.
    supports_dynamic_schema = False

    # Does the DB support catalogs? A catalog here is a group of schemas, and has
    # different names depending on the DB: BigQuery calles it a "project", Postgres calls  # noqa: E501
    # it a "database", Trino calls it a "catalog", etc.
    #
    # When this is changed to true in a DB engine spec it MUST support the
    # `get_default_catalog` and `get_catalog_names` methods. In addition, you MUST write
    # a database migration updating any existing schema permissions using the helper
    # `upgrade_catalog_perms`.
    supports_catalog = False

    # Can the catalog be changed on a per-query basis?
    supports_dynamic_catalog = False

    # Does the DB engine spec support cross-catalog queries?
    supports_cross_catalog_queries = False

    # Does the engine supports OAuth 2.0? This requires logic to be added to one of the
    # the user impersonation methods to handle personal tokens.
    supports_oauth2 = False
    oauth2_scope = ""
    oauth2_authorization_request_uri: str | None = None  # pylint: disable=invalid-name
    oauth2_token_request_uri: str | None = None
    oauth2_token_request_type = "data"  # noqa: S105

    # Driver-specific exception that should be mapped to OAuth2RedirectError
    oauth2_exception = OAuth2RedirectError

    # Does the query id related to the connection?
    # The default value is True, which means that the query id is determined when
    # the connection is created.
    # When this is changed to false in a DB engine spec it means the query id
    # is determined only after the specific query is executed and it will update
    # the `cancel_query` value in the `extra` field of the `query` object
    has_query_id_before_execute = True

    @classmethod
    def get_rls_method(cls) -> RLSMethod:
        """
        Returns the RLS method to be used for this engine.

        There are two ways to insert RLS: either replacing the table with a subquery
        that has the RLS, or appending the RLS to the ``WHERE`` clause. The former is
        safer, but not supported in all databases.
        """
        return (
            RLSMethod.AS_SUBQUERY
            if cls.allows_subqueries and cls.allows_alias_in_select
            else RLSMethod.AS_PREDICATE
        )

    @classmethod
    def is_oauth2_enabled(cls) -> bool:
        return (
            cls.supports_oauth2
            and cls.engine_name in app.config["DATABASE_OAUTH2_CLIENTS"]
        )

    @classmethod
    def start_oauth2_dance(cls, database: Database) -> None:
        """
        Start the OAuth2 dance.

        This method will raise a custom exception that is captured by the frontend to
        start the OAuth2 authentication. The frontend will open a new tab where the user
        can authorize Superset to access the database. Once the user has authorized, the
        tab sends a message to the original tab informing that authorization was
        successful (or not), and then closes. The original tab will automatically
        re-run the query after authorization.
        """
        tab_id = str(uuid4())
        default_redirect_uri = url_for("DatabaseRestApi.oauth2", _external=True)

        # The state is passed to the OAuth2 provider, and sent back to Superset after
        # the user authorizes the access. The redirect endpoint in Superset can then
        # inspect the state to figure out to which user/database the access token
        # belongs to.
        state: OAuth2State = {
            # Database ID and user ID are the primary key associated with the token.
            "database_id": database.id,
            "user_id": g.user.id,
            # In multi-instance deployments there might be a single proxy handling
            # redirects, with a custom `DATABASE_OAUTH2_REDIRECT_URI`. Since the OAuth2
            # application requires every redirect URL to be registered a priori, this
            # allows OAuth2 to be used where new instances are being constantly
            # deployed. The proxy can extract `default_redirect_uri` from the state and
            # then forward the token to the instance that initiated the authentication.
            "default_redirect_uri": default_redirect_uri,
            # When OAuth2 is complete the browser tab where OAuth2 happened will send a
            # message to the original browser tab informing that the process was
            # successful. To allow cross-tab commmunication in a safe way we assign a
            # UUID to the original tab, and the second tab will use it when sending the
            # message.
            "tab_id": tab_id,
        }
        oauth2_config = database.get_oauth2_config()
        if oauth2_config is None:
            raise OAuth2Error("No configuration found for OAuth2")

        oauth_url = cls.get_oauth2_authorization_uri(oauth2_config, state)

        raise OAuth2RedirectError(oauth_url, tab_id, default_redirect_uri)

    @classmethod
    def get_oauth2_config(cls) -> OAuth2ClientConfig | None:
        """
        Build the DB engine spec level OAuth2 client config.
        """
        oauth2_config = app.config["DATABASE_OAUTH2_CLIENTS"]
        if cls.engine_name not in oauth2_config:
            return None

        db_engine_spec_config = oauth2_config[cls.engine_name]
        redirect_uri = app.config.get(
            "DATABASE_OAUTH2_REDIRECT_URI",
            url_for("DatabaseRestApi.oauth2", _external=True),
        )

        config: OAuth2ClientConfig = {
            "id": db_engine_spec_config["id"],
            "secret": db_engine_spec_config["secret"],
            "scope": db_engine_spec_config.get("scope") or cls.oauth2_scope,
            "redirect_uri": redirect_uri,
            "authorization_request_uri": db_engine_spec_config.get(
                "authorization_request_uri",
                cls.oauth2_authorization_request_uri,
            ),
            "token_request_uri": db_engine_spec_config.get(
                "token_request_uri",
                cls.oauth2_token_request_uri,
            ),
            "request_content_type": db_engine_spec_config.get(
                "request_content_type", cls.oauth2_token_request_type
            ),
        }

        return config

    @classmethod
    def get_oauth2_authorization_uri(
        cls,
        config: OAuth2ClientConfig,
        state: OAuth2State,
    ) -> str:
        """
        Return URI for initial OAuth2 request.
        """
        uri = config["authorization_request_uri"]
        params = {
            "scope": config["scope"],
            "access_type": "offline",
            "include_granted_scopes": "false",
            "response_type": "code",
            "state": encode_oauth2_state(state),
            "redirect_uri": config["redirect_uri"],
            "client_id": config["id"],
            "prompt": "consent",
        }
        return urljoin(uri, "?" + urlencode(params))

    @classmethod
    def get_oauth2_token(
        cls,
        config: OAuth2ClientConfig,
        code: str,
    ) -> OAuth2TokenResponse:
        """
        Exchange authorization code for refresh/access tokens.
        """
        timeout = app.config["DATABASE_OAUTH2_TIMEOUT"].total_seconds()
        uri = config["token_request_uri"]
        req_body = {
            "code": code,
            "client_id": config["id"],
            "client_secret": config["secret"],
            "redirect_uri": config["redirect_uri"],
            "grant_type": "authorization_code",
        }
        if config["request_content_type"] == "data":
            return requests.post(uri, data=req_body, timeout=timeout).json()
        return requests.post(uri, json=req_body, timeout=timeout).json()

    @classmethod
    def get_oauth2_fresh_token(
        cls,
        config: OAuth2ClientConfig,
        refresh_token: str,
    ) -> OAuth2TokenResponse:
        """
        Refresh an access token that has expired.
        """
        timeout = app.config["DATABASE_OAUTH2_TIMEOUT"].total_seconds()
        uri = config["token_request_uri"]
        req_body = {
            "client_id": config["id"],
            "client_secret": config["secret"],
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        if config["request_content_type"] == "data":
            return requests.post(uri, data=req_body, timeout=timeout).json()
        return requests.post(uri, json=req_body, timeout=timeout).json()

    @classmethod
    def get_allows_alias_in_select(
        cls,
        database: Database,  # pylint: disable=unused-argument
    ) -> bool:
        """
        Method for dynamic `allows_alias_in_select`.

        In Dremio this atribute is version-dependent, so Superset needs to inspect the
        database configuration in order to determine it. This method allows engine-specs
        to define dynamic values for the attribute.
        """
        return cls.allows_alias_in_select

    @classmethod
    def supports_url(cls, url: URL) -> bool:
        """
        Returns true if the DB engine spec supports a given SQLAlchemy URL.

        As an example, if a given DB engine spec has:

            class PostgresDBEngineSpec:
                engine = "postgresql"
                engine_aliases = "postgres"
                drivers = {
                    "psycopg2": "The default Postgres driver",
                    "asyncpg": "An asynchronous Postgres driver",
                }

        It would be used for all the following SQLAlchemy URIs:

            - postgres://user:password@host/db
            - postgresql://user:password@host/db
            - postgres+asyncpg://user:password@host/db
            - postgres+psycopg2://user:password@host/db
            - postgresql+asyncpg://user:password@host/db
            - postgresql+psycopg2://user:password@host/db

        Note that SQLAlchemy has a default driver even if one is not specified:

            >>> from sqlalchemy.engine.url import make_url
            >>> make_url('postgres://').get_driver_name()
            'psycopg2'

        """
        backend = url.get_backend_name()
        driver = url.get_driver_name()
        return cls.supports_backend(backend, driver)

    @classmethod
    def supports_backend(cls, backend: str, driver: str | None = None) -> bool:
        """
        Returns true if the DB engine spec supports a given SQLAlchemy backend/driver.
        """
        # check the backend first
        if backend != cls.engine and backend not in cls.engine_aliases:
            return False

        # originally DB engine specs didn't declare any drivers and the check was made
        # only on the engine; if that's the case, ignore the driver for backwards
        # compatibility
        if not cls.drivers or driver is None:
            return True

        return driver in cls.drivers

    @classmethod
    def get_default_catalog(
        cls,
        database: Database,  # pylint: disable=unused-argument
    ) -> str | None:
        """
        Return the default catalog for a given database.
        """
        return None

    @classmethod
    def get_default_schema(cls, database: Database, catalog: str | None) -> str | None:
        """
        Return the default schema for a catalog in a given database.
        """
        with database.get_inspector(catalog=catalog) as inspector:
            return inspector.default_schema_name

    @classmethod
    def get_schema_from_engine_params(  # pylint: disable=unused-argument
        cls,
        sqlalchemy_uri: URL,
        connect_args: dict[str, Any],
    ) -> str | None:
        """
        Return the schema configured in a SQLALchemy URI and connection arguments, if any.
        """  # noqa: E501
        return None

    @classmethod
    def get_default_schema_for_query(
        cls,
        database: Database,
        query: Query,
    ) -> str | None:
        """
        Return the default schema for a given query.

        This is used to determine the schema of tables that aren't fully qualified, eg:

            SELECT * FROM foo;

        In the example above, the schema where the `foo` table lives depends on a few
        factors:

            1. For DB engine specs that allow dynamically changing the schema based on the
               query we should use the query schema.
            2. For DB engine specs that don't support dynamically changing the schema and
               have the schema hardcoded in the SQLAlchemy URI we should use the schema
               from the URI.
            3. For DB engine specs that don't connect to a specific schema and can't
               change it dynamically we need to probe the database for the default schema.

        Determining the correct schema is crucial for managing access to data, so please
        make sure you understand this logic when working on a new DB engine spec.
        """  # noqa: E501
        # dynamic schema varies on a per-query basis
        if cls.supports_dynamic_schema:
            return query.schema

        # check if the schema is stored in the SQLAlchemy URI or connection arguments
        try:
            connect_args = database.get_extra()["engine_params"]["connect_args"]
        except KeyError:
            connect_args = {}
        sqlalchemy_uri = make_url_safe(database.sqlalchemy_uri)
        if schema := cls.get_schema_from_engine_params(sqlalchemy_uri, connect_args):
            return schema

        # return the default schema of the database
        return cls.get_default_schema(database, query.catalog)

    @classmethod
    def get_dbapi_exception_mapping(cls) -> dict[type[Exception], type[Exception]]:
        """
        Each engine can implement and converge its own specific exceptions into
        Superset DBAPI exceptions

        Note: On python 3.9 this method can be changed to a classmethod property
        without the need of implementing a metaclass type

        :return: A map of driver specific exception to superset custom exceptions
        """
        return {}

    @classmethod
    def parse_error_exception(cls, exception: Exception) -> Exception:
        """
        Each engine can implement and converge its own specific parser method

        :return: An Exception with a parsed string off the original exception
        """
        return exception

    @classmethod
    def get_dbapi_mapped_exception(cls, exception: Exception) -> Exception:
        """
        Get a superset custom DBAPI exception from the driver specific exception.

        Override if the engine needs to perform extra changes to the exception, for
        example change the exception message or implement custom more complex logic

        :param exception: The driver specific exception
        :return: Superset custom DBAPI exception
        """
        new_exception = cls.get_dbapi_exception_mapping().get(type(exception))
        if not new_exception:
            return cls.parse_error_exception(exception)
        return new_exception(str(exception))

    @classmethod
    def get_allow_cost_estimate(  # pylint: disable=unused-argument
        cls,
        extra: dict[str, Any],
    ) -> bool:
        return False

    @classmethod
    def get_text_clause(cls, clause: str) -> TextClause:
        """
        SQLAlchemy wrapper to ensure text clauses are escaped properly

        :param clause: string clause with potentially unescaped characters
        :return: text clause with escaped characters
        """
        if cls.allows_escaped_colons:
            clause = clause.replace(":", "\\:")
        return text(clause)

    @classmethod
    def get_engine(
        cls,
        database: Database,
        catalog: str | None = None,
        schema: str | None = None,
        source: utils.QuerySource | None = None,
    ) -> ContextManager[Engine]:
        """
        Return an engine context manager.

            >>> with DBEngineSpec.get_engine(database, catalog, schema, source) as engine:
            ...     connection = engine.connect()
            ...     connection.execute(sql)

        """  # noqa: E501
        return database.get_sqla_engine(catalog=catalog, schema=schema, source=source)

    @classmethod
    def get_timestamp_expr(
        cls,
        col: ColumnClause,
        pdf: str | None,
        time_grain: str | None,
    ) -> TimestampExpression:
        """
        Construct a TimestampExpression to be used in a SQLAlchemy query.

        :param col: Target column for the TimestampExpression
        :param pdf: date format (seconds or milliseconds)
        :param time_grain: time grain, e.g. P1Y for 1 year
        :return: TimestampExpression object
        """
        if time_grain:
            type_ = str(getattr(col, "type", ""))
            time_expr = cls.get_time_grain_expressions().get(time_grain)
            if not time_expr:
                raise NotImplementedError(
                    f"No grain spec for {time_grain} for database {cls.engine}"
                )
            if type_ and "{func}" in time_expr:
                date_trunc_function = cls._date_trunc_functions.get(type_)
                if date_trunc_function:
                    time_expr = time_expr.replace("{func}", date_trunc_function)
            if type_ and "{type}" in time_expr:
                date_trunc_function = cls._date_trunc_functions.get(type_)
                if date_trunc_function:
                    time_expr = time_expr.replace("{type}", type_)
        else:
            time_expr = "{col}"

        # if epoch, translate to DATE using db specific conf
        if pdf == "epoch_s":
            time_expr = time_expr.replace("{col}", cls.epoch_to_dttm())
        elif pdf == "epoch_ms":
            time_expr = time_expr.replace("{col}", cls.epoch_ms_to_dttm())

        return TimestampExpression(time_expr, col, type_=col.type)

    @classmethod
    def get_time_grains(cls) -> tuple[TimeGrain, ...]:
        """
        Generate a tuple of supported time grains.

        :return: All time grains supported by the engine
        """

        ret_list = []
        time_grains = builtin_time_grains.copy()
        time_grains.update(app.config["TIME_GRAIN_ADDONS"])
        for duration, func in cls.get_time_grain_expressions().items():
            if duration in time_grains:
                name = time_grains[duration]
                ret_list.append(TimeGrain(name, _(name), func, duration))
        return tuple(ret_list)

    @classmethod
    def _sort_time_grains(
        cls, val: tuple[str | None, str], index: int
    ) -> float | int | str:
        """
        Return an ordered time-based value of a portion of a time grain
        for sorting
        Values are expected to be either None or start with P or PT
        Have a numerical value in the middle and end with
        a value for the time interval
        It can also start or end with epoch start time denoting a range
        i.e, week beginning or ending with a day
        """
        pos = {
            "FIRST": 0,
            "SECOND": 1,
            "THIRD": 2,
            "LAST": 3,
        }

        if val[0] is None:
            return pos["FIRST"]

        prog = re.compile(r"(.*\/)?(P|PT)([0-9\.]+)(S|M|H|D|W|M|Y)(\/.*)?")
        result = prog.match(val[0])

        # for any time grains that don't match the format, put them at the end
        if result is None:
            return pos["LAST"]

        second_minute_hour = ["S", "M", "H"]
        day_week_month_year = ["D", "W", "M", "Y"]
        is_less_than_day = result.group(2) == "PT"
        interval = result.group(4)
        epoch_time_start_string = result.group(1) or result.group(5)
        has_starting_or_ending = bool(len(epoch_time_start_string or ""))

        def sort_day_week() -> int:
            if has_starting_or_ending:
                return pos["LAST"]
            if is_less_than_day:
                return pos["SECOND"]
            return pos["THIRD"]

        def sort_interval() -> float:
            if is_less_than_day:
                return second_minute_hour.index(interval)
            return day_week_month_year.index(interval)

        # 0: all "PT" values should come before "P" values (i.e, PT10M)
        # 1: order values within the above arrays ("D" before "W")
        # 2: sort by numeric value (PT10M before PT15M)
        # 3: sort by any week starting/ending values
        plist = {
            0: sort_day_week(),
            1: pos["SECOND"] if is_less_than_day else pos["THIRD"],
            2: sort_interval(),
            3: float(result.group(3)),
        }

        return plist.get(index, 0)

    @classmethod
    def get_time_grain_expressions(cls) -> dict[str | None, str]:
        """
        Return a dict of all supported time grains including any potential added grains
        but excluding any potentially disabled grains in the config file.

        :return: All time grain expressions supported by the engine
        """
        # TODO: use @memoize decorator or similar to avoid recomputation on every call
        time_grain_expressions = cls._time_grain_expressions.copy()
        grain_addon_expressions = app.config["TIME_GRAIN_ADDON_EXPRESSIONS"]
        time_grain_expressions.update(grain_addon_expressions.get(cls.engine, {}))
        denylist: list[str] = app.config["TIME_GRAIN_DENYLIST"]
        for key in denylist:
            time_grain_expressions.pop(key, None)

        return dict(
            sorted(
                time_grain_expressions.items(),
                key=lambda x: (
                    cls._sort_time_grains(x, 0),
                    cls._sort_time_grains(x, 1),
                    cls._sort_time_grains(x, 2),
                    cls._sort_time_grains(x, 3),
                ),
            )
        )

    @classmethod
    def fetch_data(cls, cursor: Any, limit: int | None = None) -> list[tuple[Any, ...]]:
        """

        :param cursor: Cursor instance
        :param limit: Maximum number of rows to be returned by the cursor
        :return: Result of query
        """
        if cls.arraysize:
            cursor.arraysize = cls.arraysize
        try:
            if cls.limit_method == LimitMethod.FETCH_MANY and limit:
                return cursor.fetchmany(limit)
            data = cursor.fetchall()
            description = cursor.description or []
            # Create a mapping between column name and a mutator function to normalize
            # values with. The first two items in the description row are
            # the column name and type.
            column_mutators = {
                row[0]: func
                for row in description
                if (
                    func := cls.column_type_mutators.get(
                        type(cls.get_sqla_column_type(cls.get_datatype(row[1])))
                    )
                )
            }
            if column_mutators:
                indexes = {row[0]: idx for idx, row in enumerate(description)}
                for row_idx, row in enumerate(data):
                    new_row = list(row)
                    for col, func in column_mutators.items():
                        col_idx = indexes[col]
                        new_row[col_idx] = func(row[col_idx])
                    data[row_idx] = tuple(new_row)

            return data
        except Exception as ex:
            raise cls.get_dbapi_mapped_exception(ex) from ex

    @classmethod
    def expand_data(
        cls, columns: list[ResultSetColumnType], data: list[dict[Any, Any]]
    ) -> tuple[
        list[ResultSetColumnType], list[dict[Any, Any]], list[ResultSetColumnType]
    ]:
        """
        Some engines support expanding nested fields. See implementation in Presto
        spec for details.

        :param columns: columns selected in the query
        :param data: original data set
        :return: list of all columns(selected columns and their nested fields),
                 expanded data set, listed of nested fields
        """
        return columns, data, []

    @classmethod
    def alter_new_orm_column(cls, orm_col: TableColumn) -> None:
        """Allow altering default column attributes when first detected/added

        For instance special column like `__time` for Druid can be
        set to is_dttm=True. Note that this only gets called when new
        columns are detected/created"""

    @classmethod
    def epoch_to_dttm(cls) -> str:
        """
        SQL expression that converts epoch (seconds) to datetime that can be used in a
        query. The reference column should be denoted as `{col}` in the return
        expression, e.g. "FROM_UNIXTIME({col})"

        :return: SQL Expression
        """
        raise NotImplementedError()

    @classmethod
    def epoch_ms_to_dttm(cls) -> str:
        """
        SQL expression that converts epoch (milliseconds) to datetime that can be used
        in a query.

        :return: SQL Expression
        """
        return cls.epoch_to_dttm().replace("{col}", "({col}/1000)")

    @classmethod
    def get_datatype(cls, type_code: Any) -> str | None:
        """
        Change column type code from cursor description to string representation.

        :param type_code: Type code from cursor description
        :return: String representation of type code
        """
        if isinstance(type_code, str) and type_code != "":
            return type_code.upper()
        return None

    @classmethod
    @deprecated(deprecated_in="3.0")
    def normalize_indexes(cls, indexes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Normalizes indexes for more consistency across db engines

        noop by default

        :param indexes: Raw indexes as returned by SQLAlchemy
        :return: cleaner, more aligned index definition
        """
        return indexes

    @classmethod
    def get_table_metadata(
        cls,
        database: Database,
        table: Table,
    ) -> TableMetadataResponse:
        """
        Returns basic table metadata

        :param database: Database instance
        :param table: A Table instance
        :return: Basic table metadata
        """
        return get_table_metadata(database, table)

    @classmethod
    def get_extra_table_metadata(
        cls,
        database: Database,
        table: Table,
    ) -> dict[str, Any]:
        """
        Returns engine-specific table metadata

        :param database: Database instance
        :param table: A Table instance
        :return: Engine-specific table metadata
        """
        # old method that doesn't work with catalogs
        if hasattr(cls, "extra_table_metadata"):
            warnings.warn(  # noqa: B028
                "The `extra_table_metadata` method is deprecated, please implement "
                "the `get_extra_table_metadata` method in the DB engine spec.",
                DeprecationWarning,
            )

            # If a catalog is passed, return nothing, since we don't know the exact
            # table that is being requested.
            if table.catalog:
                return {}

            return cls.extra_table_metadata(database, table.table, table.schema)

        return {}

    @classmethod
    def get_limit_from_sql(cls, sql: str) -> int | None:
        """
        Extract limit from SQL query

        :param sql: SQL query
        :return: Value of limit clause in query
        """
        script = SQLScript(sql, engine=cls.engine)
        return script.statements[-1].get_limit_value()

    @classmethod
    def get_cte_query(cls, sql: str) -> str | None:
        """
        Convert the input CTE based SQL to the SQL for virtual table conversion

        :param sql: SQL query
        :return: CTE with the main select query aliased as `__cte`

        """
        if not cls.allows_cte_in_subquery:
            statement = SQLStatement(sql, engine=cls.engine)
            if statement.has_cte():
                return statement.as_cte(cls.cte_alias).format()

        return None

    @classmethod
    def df_to_sql(
        cls,
        database: Database,
        table: Table,
        df: pd.DataFrame,
        to_sql_kwargs: dict[str, Any],
    ) -> None:
        """
        Upload data from a Pandas DataFrame to a database.

        For regular engines this calls the `pandas.DataFrame.to_sql` method. Can be
        overridden for engines that don't work well with this method, e.g. Hive and
        BigQuery.

        Note this method does not create metadata for the table.

        :param database: The database to upload the data to
        :param table: The table to upload the data to
        :param df: The dataframe with data to be uploaded
        :param to_sql_kwargs: The kwargs to be passed to pandas.DataFrame.to_sql` method
        """

        to_sql_kwargs["name"] = table.table

        if table.schema:
            # Only add schema when it is preset and non-empty.
            to_sql_kwargs["schema"] = table.schema

        with cls.get_engine(
            database,
            catalog=table.catalog,
            schema=table.schema,
        ) as engine:
            if (
                engine.dialect.supports_multivalues_insert
                or cls.supports_multivalues_insert
            ):
                to_sql_kwargs["method"] = "multi"
            df.to_sql(con=engine, **to_sql_kwargs)

    @classmethod
    def convert_dttm(  # pylint: disable=unused-argument
        cls, target_type: str, dttm: datetime, db_extra: dict[str, Any] | None = None
    ) -> str | None:
        """
        Convert a Python `datetime` object to a SQL expression.

        :param target_type: The target type of expression
        :param dttm: The datetime object
        :param db_extra: The database extra object
        :return: The SQL expression
        """
        return None

    @classmethod
    def handle_boolean_filter(
        cls, sqla_col: Any, op: str, value: bool
    ) -> BinaryExpression:
        """
        Handle boolean filter operations with engine-specific logic.

        By default, uses SQLAlchemy's IS operator (column IS true/false).
        Engines that don't support IS for boolean values can override
        use_equality_for_boolean_filters to use equality operators instead.

        :param sqla_col: SQLAlchemy column element
        :param op: Filter operator (IS_TRUE or IS_FALSE)
        :param value: Boolean value (True or False)
        :return: SQLAlchemy expression for the boolean filter
        """
        if cls.use_equality_for_boolean_filters:
            return sqla_col == value
        else:
            return sqla_col.is_(value)

    @classmethod
    def handle_null_filter(
        cls,
        sqla_col: Any,
        op: utils.FilterOperator,
    ) -> BinaryExpression:
        """
        Handle null/not null filter operations.

        :param sqla_col: SQLAlchemy column element
        :param op: Filter operator (IS_NULL or IS_NOT_NULL)
        :return: SQLAlchemy expression for the null filter
        """
        from superset.utils import core as utils

        if op == utils.FilterOperator.IS_NULL:
            return sqla_col.is_(None)
        elif op == utils.FilterOperator.IS_NOT_NULL:
            return sqla_col.isnot(None)
        else:
            raise ValueError(f"Invalid null filter operator: {op}")

    @classmethod
    def handle_comparison_filter(
        cls, sqla_col: Any, op: utils.FilterOperator, value: Any
    ) -> BinaryExpression:
        """
        Handle comparison filter operations (=, !=, >, <, >=, <=).

        :param sqla_col: SQLAlchemy column element
        :param op: Filter operator
        :param value: Filter value
        :return: SQLAlchemy expression for the comparison filter
        """
        from superset.utils import core as utils

        if op == utils.FilterOperator.EQUALS:
            return sqla_col == value
        elif op == utils.FilterOperator.NOT_EQUALS:
            return sqla_col != value
        elif op == utils.FilterOperator.GREATER_THAN:
            return sqla_col > value
        elif op == utils.FilterOperator.LESS_THAN:
            return sqla_col < value
        elif op == utils.FilterOperator.GREATER_THAN_OR_EQUALS:
            return sqla_col >= value
        elif op == utils.FilterOperator.LESS_THAN_OR_EQUALS:
            return sqla_col <= value
        else:
            raise ValueError(f"Invalid comparison filter operator: {op}")

    @classmethod
    def handle_cursor(cls, cursor: Any, query: Query) -> None:
        """Handle a live cursor between the execute and fetchall calls

        The flow works without this method doing anything, but it allows
        for handling the cursor and updating progress information in the
        query object"""

    @classmethod
    # pylint: disable=consider-using-transaction
    def execute_with_cursor(
        cls,
        cursor: Any,
        sql: str,
        query: Query,
    ) -> None:
        """
        Trigger execution of a query and handle the resulting cursor.

        For most implementations this just makes calls to `execute` and
        `handle_cursor` consecutively, but in some engines (e.g. Trino) we may
        need to handle client limitations such as lack of async support and
        perform a more complicated operation to get information from the cursor
        in a timely manner and facilitate operations such as query stop
        """
        logger.debug("Query %d: Running query: %s", query.id, sql)
        cls.execute(cursor, sql, query.database, async_=True)
        if not cls.has_query_id_before_execute:
            cancel_query_id = query.database.db_engine_spec.get_cancel_query_id(
                cursor, query
            )
            if cancel_query_id is not None:
                query.set_extra_json_key(QUERY_CANCEL_KEY, cancel_query_id)
                db.session.commit()
        logger.debug("Query %d: Handling cursor", query.id)
        cls.handle_cursor(cursor, query)

    @classmethod
    def extract_error_message(cls, ex: Exception) -> str:
        return f"{cls.engine} error: {cls._extract_error_message(ex)}"

    @classmethod
    def _extract_error_message(cls, ex: Exception) -> str:
        """Extract error message for queries"""
        return utils.error_msg_from_exception(ex)

    @classmethod
    def extract_errors(
        cls, ex: Exception, context: dict[str, Any] | None = None
    ) -> list[SupersetError]:
        raw_message = cls._extract_error_message(ex)

        context = context or {}
        for regex, (message, error_type, extra) in cls.custom_errors.items():
            if match := regex.search(raw_message):
                params = {**context, **match.groupdict()}
                extra["engine_name"] = cls.engine_name
                return [
                    SupersetError(
                        error_type=error_type,
                        message=message % params,
                        level=ErrorLevel.ERROR,
                        extra=extra,
                    )
                ]

        return [
            SupersetError(
                error_type=SupersetErrorType.GENERIC_DB_ENGINE_ERROR,
                message=cls._extract_error_message(ex),
                level=ErrorLevel.ERROR,
                extra={"engine_name": cls.engine_name},
            )
        ]

    @classmethod
    def adjust_engine_params(  # pylint: disable=unused-argument
        cls,
        uri: URL,
        connect_args: dict[str, Any],
        catalog: str | None = None,
        schema: str | None = None,
    ) -> tuple[URL, dict[str, Any]]:
        """
        Return a new URL and ``connect_args`` for a specific catalog/schema.

        This is used in SQL Lab, allowing users to select a schema from the list of
        schemas available in a given database, and have the query run with that schema as
        the default one.

        For some databases (like MySQL, Presto, Snowflake) this requires modifying the
        SQLAlchemy URI before creating the connection. For others (like Postgres), it
        requires additional parameters in ``connect_args`` or running pre-session
        queries with ``set`` parameters.

        When a DB engine spec implements this method or ``get_prequeries`` (see below) it
        should also have the attribute ``supports_dynamic_schema`` set to true, so that
        Superset knows in which schema a given query is running in order to enforce
        permissions (see #23385 and #23401).
        """  # noqa: E501
        return uri, {
            **connect_args,
            **cls.enforce_uri_query_params.get(uri.get_driver_name(), {}),
        }

    @classmethod
    def get_prequeries(
        cls,
        database: Database,  # pylint: disable=unused-argument
        catalog: str | None = None,  # pylint: disable=unused-argument
        schema: str | None = None,  # pylint: disable=unused-argument
    ) -> list[str]:
        """
        Return pre-session queries.

        These are currently used as an alternative to ``adjust_engine_params`` for
        databases where the selected schema cannot be specified in the SQLAlchemy URI or
        connection arguments.

        For example, in order to specify a default schema in RDS we need to run a query
        at the beginning of the session:

            sql> set search_path = my_schema;

        """
        return []

    @classmethod
    def patch(cls) -> None:
        """
        TODO: Improve docstring and refactor implementation in Hive
        """

    @classmethod
    def get_catalog_names(  # pylint: disable=unused-argument
        cls,
        database: Database,
        inspector: Inspector,
    ) -> set[str]:
        """
        Get all catalogs from database.

        This needs to be implemented per database, since SQLAlchemy doesn't offer an
        abstraction.
        """
        return set()

    @classmethod
    def get_schema_names(cls, inspector: Inspector) -> set[str]:
        """
        Get all schemas from database

        :param inspector: SqlAlchemy inspector
        :return: All schemas in the database
        """
        return set(inspector.get_schema_names())

    @classmethod
    def get_table_names(  # pylint: disable=unused-argument
        cls,
        database: Database,
        inspector: Inspector,
        schema: str | None,
    ) -> set[str]:
        """
        Get all the real table names within the specified schema.

        Per the SQLAlchemy definition if the schema is omitted the database’s default
        schema is used, however some dialects infer the request as schema agnostic.

        :param database: The database to inspect
        :param inspector: The SQLAlchemy inspector
        :param schema: The schema to inspect
        :returns: The physical table names
        """

        try:
            tables = set(inspector.get_table_names(schema))
        except Exception as ex:
            raise cls.get_dbapi_mapped_exception(ex) from ex

        if schema and cls.try_remove_schema_from_table_name:
            tables = {re.sub(f"^{schema}\\.", "", table) for table in tables}
        return tables

    @classmethod
    def get_view_names(  # pylint: disable=unused-argument
        cls,
        database: Database,
        inspector: Inspector,
        schema: str | None,
    ) -> set[str]:
        """
        Get all the view names within the specified schema.

        Per the SQLAlchemy definition if the schema is omitted the database’s default
        schema is used, however some dialects infer the request as schema agnostic.

        :param database: The database to inspect
        :param inspector: The SQLAlchemy inspector
        :param schema: The schema to inspect
        :returns: The view names
        """

        try:
            views = set(inspector.get_view_names(schema))
        except Exception as ex:
            raise cls.get_dbapi_mapped_exception(ex) from ex

        if schema and cls.try_remove_schema_from_table_name:
            views = {re.sub(f"^{schema}\\.", "", view) for view in views}
        return views

    @classmethod
    def get_indexes(
        cls,
        database: Database,  # pylint: disable=unused-argument
        inspector: Inspector,
        table: Table,
    ) -> list[dict[str, Any]]:
        """
        Get the indexes associated with the specified schema/table.

        :param database: The database to inspect
        :param inspector: The SQLAlchemy inspector
        :param table: The table instance to inspect
        :returns: The indexes
        """

        return inspector.get_indexes(table.table, table.schema)

    @classmethod
    def get_table_comment(
        cls,
        inspector: Inspector,
        table: Table,
    ) -> str | None:
        """
        Get comment of table from a given schema and table
        :param inspector: SqlAlchemy Inspector instance
        :param table: Table instance
        :return: comment of table
        """
        comment = None
        try:
            comment = inspector.get_table_comment(table.table, table.schema)
            comment = comment.get("text") if isinstance(comment, dict) else None
        except NotImplementedError:
            # It's expected that some dialects don't implement the comment method
            pass
        except Exception as ex:  # pylint: disable=broad-except
            logger.error("Unexpected error while fetching table comment", exc_info=True)
            logger.exception(ex)
        return comment

    @classmethod
    def get_columns(  # pylint: disable=unused-argument
        cls,
        inspector: Inspector,
        table: Table,
        options: dict[str, Any] | None = None,
    ) -> list[ResultSetColumnType]:
        """
        Get all columns from a given schema and table.

        The inspector will be bound to a catalog, if one was specified.

        :param inspector: SqlAlchemy Inspector instance
        :param table: Table instance
        :param options: Extra options to customise the display of columns in
                        some databases
        :return: All columns in table
        """
        return convert_inspector_columns(
            cast(
                list[SQLAColumnType],
                inspector.get_columns(table.table, table.schema),
            )
        )

    @classmethod
    def get_metrics(  # pylint: disable=unused-argument
        cls,
        database: Database,
        inspector: Inspector,
        table: Table,
    ) -> list[MetricType]:
        """
        Get all metrics from a given schema and table.
        """
        return [
            {
                "metric_name": "count",
                "verbose_name": "COUNT(*)",
                "metric_type": "count",
                "expression": "COUNT(*)",
            }
        ]

    @classmethod
    def where_latest_partition(  # pylint: disable=unused-argument
        cls,
        database: Database,
        table: Table,
        query: Select,
        columns: list[ResultSetColumnType] | None = None,
    ) -> Select | None:
        """
        Add a where clause to a query to reference only the most recent partition

        :param table: Table instance
        :param database: Database instance
        :param query: SqlAlchemy query
        :param columns: List of TableColumns
        :return: SqlAlchemy query with additional where clause referencing the latest
        partition
        """
        # TODO: Fix circular import caused by importing Database, TableColumn
        return None

    @classmethod
    def _get_fields(cls, cols: list[ResultSetColumnType]) -> list[Any]:
        return [
            (
                literal_column(query_as)
                if (query_as := c.get("query_as"))
                else column(c["column_name"])
            )
            for c in cols
        ]

    @classmethod
    def select_star(  # pylint: disable=too-many-arguments
        cls,
        database: Database,
        table: Table,
        engine: Engine,
        limit: int = 100,
        show_cols: bool = False,
        indent: bool = True,
        latest_partition: bool = True,
        cols: list[ResultSetColumnType] | None = None,
    ) -> str:
        """
        Generate a "SELECT * from [catalog.][schema.]table_name" query with limit.

        WARNING: expects only unquoted table and schema names.

        :param database: Database instance
        :param table: Table instance
        :param engine: SqlAlchemy Engine instance
        :param limit: limit to impose on query
        :param show_cols: Show columns in query; otherwise use "*"
        :param indent: Add indentation to query
        :param latest_partition: Only query the latest partition
        :param cols: Columns to include in query
        :return: SQL query
        """
        if not cls.supports_cross_catalog_queries:
            table = Table(table.table, table.schema, None)

        # pylint: disable=redefined-outer-name
        fields: str | list[Any] = "*"
        cols = cols or []
        if (show_cols or latest_partition) and not cols:
            cols = database.get_columns(table)

        if show_cols:
            fields = cls._get_fields(cols)

        full_table_name = cls.quote_table(table, engine.dialect)
        qry = select(fields).select_from(text(full_table_name))

        qry = qry.limit(limit)
        if latest_partition:
            partition_query = cls.where_latest_partition(
                database,
                table,
                qry,
                columns=cols,
            )
            if partition_query is not None:
                qry = partition_query
        sql = database.compile_sqla_query(qry, table.catalog, table.schema)
        if indent:
            sql = SQLScript(sql, engine=cls.engine).format()
        return sql

    @classmethod
    def estimate_statement_cost(
        cls, database: Database, statement: str, cursor: Any
    ) -> dict[str, Any]:
        """
        Generate a SQL query that estimates the cost of a given statement.

        :param database: A Database object
        :param statement: A single SQL statement
        :param cursor: Cursor instance
        :return: Dictionary with different costs
        """
        raise Exception(  # pylint: disable=broad-exception-raised
            "Database does not support cost estimation"
        )

    @classmethod
    def query_cost_formatter(
        cls, raw_cost: list[dict[str, Any]]
    ) -> list[dict[str, str]]:
        """
        Format cost estimate.

        :param raw_cost: Raw estimate from `estimate_query_cost`
        :return: Human readable cost estimate
        """
        raise Exception(  # pylint: disable=broad-exception-raised
            "Database does not support cost estimation"
        )

    @classmethod
    def process_statement(
        cls,
        statement: BaseSQLStatement[Any],
        database: Database,
    ) -> str:
        """
        Process a SQL statement by mutating it.

        :param statement: A single SQL statement
        :param database: Database instance
        :return: Dictionary with different costs
        """
        return database.mutate_sql_based_on_config(str(statement), is_split=True)

    @classmethod
    def estimate_query_cost(  # pylint: disable=too-many-arguments
        cls,
        database: Database,
        catalog: str | None,
        schema: str,
        sql: str,
        source: utils.QuerySource | None = None,
    ) -> list[dict[str, Any]]:
        """
        Estimate the cost of a multiple statement SQL query.

        :param database: Database instance
        :param schema: Database schema
        :param sql: SQL query with possibly multiple statements
        :param source: Source of the query (eg, "sql_lab")
        """
        extra = database.get_extra(source) or {}
        if not cls.get_allow_cost_estimate(extra):
            raise Exception(  # pylint: disable=broad-exception-raised
                "Database does not support cost estimation"
            )

        parsed_script = SQLScript(sql, engine=cls.engine)

        with database.get_raw_connection(
            catalog=catalog,
            schema=schema,
            source=source,
        ) as conn:
            cursor = conn.cursor()
            return [
                cls.estimate_statement_cost(
                    database,
                    cls.process_statement(statement, database),
                    cursor,
                )
                for statement in parsed_script.statements
            ]

    @classmethod
    def impersonate_user(
        cls,
        database: Database,
        username: str | None,
        user_token: str | None,
        url: URL,
        engine_kwargs: dict[str, Any],
    ) -> tuple[URL, dict[str, Any]]:
        """
        Modify URL and/or engine kwargs to impersonate a different user.
        """
        # Update URL using old methods until 6.0.0.
        url = cls.get_url_for_impersonation(url, True, username, user_token)

        # Update engine kwargs using old methods. Note that #30674 modified the method
        # signature, so we need to check if the method has the old signature.
        connect_args = engine_kwargs.setdefault("connect_args", {})
        args = [
            connect_args,
            url,
            username,
            user_token,
        ]
        if "database" in signature(cls.update_impersonation_config).parameters:
            args.insert(0, database)

        cls.update_impersonation_config(*args)

        return url, engine_kwargs

    @classmethod
    @deprecated(deprecated_in="6.0.0")
    def get_url_for_impersonation(
        cls,
        url: URL,
        impersonate_user: bool,
        username: str | None,
        access_token: str | None,  # pylint: disable=unused-argument
    ) -> URL:
        """
        Return a modified URL with the username set.

        :param url: SQLAlchemy URL object
        :param impersonate_user: Flag indicating if impersonation is enabled
        :param username: Effective username
        :param access_token: Personal access token
        """
        if impersonate_user and username is not None:
            url = url.set(username=username)

        return url

    @classmethod
    @deprecated(deprecated_in="6.0.0")
    def update_impersonation_config(  # pylint: disable=too-many-arguments
        cls,
        database: Database,
        connect_args: dict[str, Any],
        uri: str,
        username: str | None,
        access_token: str | None,
    ) -> None:
        """
        Update a configuration dictionary
        that can set the correct properties for impersonating users

        :param connect_args: a Database object
        :param connect_args: config to be updated
        :param uri: URI
        :param username: Effective username
        :param access_token: Personal access token for OAuth2
        :return: None
        """

    @classmethod
    def execute(  # pylint: disable=unused-argument
        cls,
        cursor: Any,
        query: str,
        database: Database,
        **kwargs: Any,
    ) -> None:
        """
        Execute a SQL query

        :param cursor: Cursor instance
        :param query: Query to execute
        :param database_id: ID of the database where the query will run
        :param kwargs: kwargs to be passed to cursor.execute()
        :return:
        """
        if cls.arraysize:
            cursor.arraysize = cls.arraysize
        try:
            cursor.execute(query)
        except Exception as ex:
            if database.is_oauth2_enabled() and cls.needs_oauth2(ex):
                cls.start_oauth2_dance(database)
            raise cls.get_dbapi_mapped_exception(ex) from ex

    @classmethod
    def needs_oauth2(cls, ex: Exception) -> bool:
        """
        Check if the exception is one that indicates OAuth2 is needed.
        """
        return g and hasattr(g, "user") and isinstance(ex, cls.oauth2_exception)

    @classmethod
    def make_label_compatible(cls, label: str) -> str | quoted_name:
        """
        Conditionally mutate and/or quote a sqlalchemy expression label. If
        force_column_alias_quotes is set to True, return the label as a
        sqlalchemy.sql.elements.quoted_name object to ensure that the select query
        and query results have same case. Otherwise, return the mutated label as a
        regular string. If maximum supported column name length is exceeded,
        generate a truncated label by calling truncate_label().

        :param label: expected expression label/alias
        :return: conditionally mutated label supported by the db engine
        """
        label_mutated = cls._mutate_label(label)
        if (
            cls.max_column_name_length
            and len(label_mutated) > cls.max_column_name_length
        ):
            label_mutated = cls._truncate_label(label)
        if cls.force_column_alias_quotes:
            label_mutated = quoted_name(label_mutated, True)
        return label_mutated

    @classmethod
    def get_column_types(
        cls,
        column_type: str | None,
    ) -> tuple[TypeEngine, GenericDataType] | None:
        """
        Return a sqlalchemy native column type and generic data type that corresponds
        to the column type defined in the data source (return None to use default type
        inferred by SQLAlchemy). Override `column_type_mappings` for specific needs
        (see MSSQL for example of NCHAR/NVARCHAR handling).

        :param column_type: Column type returned by inspector
        :return: SQLAlchemy and generic Superset column types
        """
        if not column_type:
            return None

        for regex, sqla_type, generic_type in (
            cls.column_type_mappings + cls._default_column_type_mappings
        ):
            match = regex.match(column_type)
            if not match:
                continue
            if callable(sqla_type):
                return sqla_type(match), generic_type
            return sqla_type, generic_type
        return None

    @staticmethod
    def _mutate_label(label: str) -> str:
        """
        Most engines support mixed case aliases that can include numbers
        and special characters, like commas, parentheses etc. For engines that
        have restrictions on what types of aliases are supported, this method
        can be overridden to ensure that labels conform to the engine's
        limitations. Mutated labels should be deterministic (input label A always
        yields output label X) and unique (input labels A and B don't yield the same
        output label X).

        :param label: Preferred expression label
        :return: Conditionally mutated label
        """
        return label

    @classmethod
    def _truncate_label(cls, label: str) -> str:
        """
        In the case that a label exceeds the max length supported by the engine,
        this method is used to construct a deterministic and unique label based on
        the original label. By default, this returns a md5 hash of the original label,
        conditionally truncated if the length of the hash exceeds the max column length
        of the engine.

        :param label: Expected expression label
        :return: Truncated label
        """
        label = md5_sha_from_str(label)
        # truncate hash if it exceeds max length
        if cls.max_column_name_length and len(label) > cls.max_column_name_length:
            label = label[: cls.max_column_name_length]
        return label

    @classmethod
    def column_datatype_to_string(
        cls, sqla_column_type: TypeEngine, dialect: Dialect
    ) -> str:
        """
        Convert sqlalchemy column type to string representation.
        By default, removes collation and character encoding info to avoid
        unnecessarily long datatypes.

        :param sqla_column_type: SqlAlchemy column type
        :param dialect: Sqlalchemy dialect
        :return: Compiled column type
        """
        sqla_column_type = sqla_column_type.copy()
        if hasattr(sqla_column_type, "collation"):
            sqla_column_type.collation = None
        if hasattr(sqla_column_type, "charset"):
            sqla_column_type.charset = None
        return sqla_column_type.compile(dialect=dialect).upper()

    @classmethod
    def get_function_names(  # pylint: disable=unused-argument
        cls,
        database: Database,
    ) -> list[str]:
        """
        Get a list of function names that are able to be called on the database.
        Used for SQL Lab autocomplete.

        :param database: The database to get functions for
        :return: A list of function names useable in the database
        """
        return []

    @staticmethod
    def pyodbc_rows_to_tuples(data: list[Any]) -> list[tuple[Any, ...]]:
        """
        Convert pyodbc.Row objects from `fetch_data` to tuples.

        :param data: List of tuples or pyodbc.Row objects
        :return: List of tuples
        """
        if data and type(data[0]).__name__ == "Row":
            data = [tuple(row) for row in data]
        return data

    @staticmethod
    def mutate_db_for_connection_test(  # pylint: disable=unused-argument
        database: Database,
    ) -> None:
        """
        Some databases require passing additional parameters for validating database
        connections. This method makes it possible to mutate the database instance prior
        to testing if a connection is ok.

        :param database: instance to be mutated
        """
        return None

    @staticmethod
    def get_extra_params(
        database: Database, source: QuerySource | None = None
    ) -> dict[str, Any]:
        """
        Some databases require adding elements to connection parameters,
        like passing certificates to `extra`. This can be done here.

        :param database: database instance from which to extract extras
        :param source: in which context is the connection needed
        :raises CertificateException: If certificate is not valid/unparseable
        """
        extra: dict[str, Any] = {}
        if database.extra:
            try:
                extra = json.loads(database.extra)
            except json.JSONDecodeError as ex:
                logger.error(ex, exc_info=True)
                raise
        return extra

    @staticmethod
    def update_params_from_encrypted_extra(  # pylint: disable=invalid-name
        database: Database, params: dict[str, Any]
    ) -> None:
        """
        Some databases require some sensitive information which do not conform to
        the username:password syntax normally used by SQLAlchemy.

        :param database: database instance from which to extract extras
        :param params: params to be updated
        """
        if not database.encrypted_extra:
            return
        try:
            encrypted_extra = json.loads(database.encrypted_extra)
            params.update(encrypted_extra)
        except json.JSONDecodeError as ex:
            logger.error(ex, exc_info=True)
            raise

    @classmethod
    def get_column_spec(  # pylint: disable=unused-argument
        cls,
        native_type: str | None,
        db_extra: dict[str, Any] | None = None,
        source: utils.ColumnTypeSource = utils.ColumnTypeSource.GET_TABLE,
    ) -> ColumnSpec | None:
        """
        Get generic type related specs regarding a native column type.

        :param native_type: Native database type
        :param db_extra: The database extra object
        :param source: Type coming from the database table or cursor description
        :return: ColumnSpec object
        """
        if col_types := cls.get_column_types(native_type):
            column_type, generic_type = col_types
            is_dttm = generic_type == GenericDataType.TEMPORAL
            return ColumnSpec(
                sqla_type=column_type, generic_type=generic_type, is_dttm=is_dttm
            )
        return None

    @classmethod
    def get_sqla_column_type(
        cls,
        native_type: str | None,
        db_extra: dict[str, Any] | None = None,
        source: utils.ColumnTypeSource = utils.ColumnTypeSource.GET_TABLE,
    ) -> TypeEngine | None:
        """
        Converts native database type to sqlalchemy column type.

        :param native_type: Native database type
        :param db_extra: The database extra object
        :param source: Type coming from the database table or cursor description
        :return: ColumnSpec object
        """
        column_spec = cls.get_column_spec(
            native_type=native_type,
            db_extra=db_extra,
            source=source,
        )
        return column_spec.sqla_type if column_spec else None

    # pylint: disable=unused-argument
    @classmethod
    def prepare_cancel_query(cls, query: Query) -> None:
        """
        Some databases may acquire the query cancelation id after the query
        cancelation request has been received. For those cases, the db engine spec
        can record the cancelation intent so that the query can either be stopped
        prior to execution, or canceled once the query id is acquired.
        """
        return None

    @classmethod
    def has_implicit_cancel(cls) -> bool:
        """
        Return True if the live cursor handles the implicit cancelation of the query,
        False otherwise.

        :return: Whether the live cursor implicitly cancels the query
        :see: handle_cursor
        """

        return False

    @classmethod
    def get_cancel_query_id(  # pylint: disable=unused-argument
        cls,
        cursor: Any,
        query: Query,
    ) -> str | None:
        """
        Select identifiers from the database engine that uniquely identifies the
        queries to cancel. The identifier is typically a session id, process id
        or similar.

        :param cursor: Cursor instance in which the query will be executed
        :param query: Query instance
        :return: Query identifier
        """

        return None

    @classmethod
    def cancel_query(  # pylint: disable=unused-argument
        cls,
        cursor: Any,
        query: Query,
        cancel_query_id: str,
    ) -> bool:
        """
        Cancel query in the underlying database.

        :param cursor: New cursor instance to the db of the query
        :param query: Query instance
        :param cancel_query_id: Value returned by get_cancel_query_payload or set in
        other life-cycle methods of the query
        :return: True if query cancelled successfully, False otherwise
        """

        return False

    @classmethod
    def get_impersonation_key(cls, user: User | None) -> Any:
        """
        Construct an impersonation key, by default it's the given username.

        :param user: logged-in user

        :returns: username if given user is not null
        """
        return user.username if user else None

    @classmethod
    def mask_encrypted_extra(cls, encrypted_extra: str | None) -> str | None:
        """
        Mask `encrypted_extra`.

        This is used to remove any sensitive data in `encrypted_extra` when presenting
        it to the user when a database is edited. For example, a private key might be
        replaced with a masked value "XXXXXXXXXX". If the masked value is changed the
        corresponding entry is updated, otherwise the old value is used (see
        `unmask_encrypted_extra` below).
        """
        if encrypted_extra is None or not cls.encrypted_extra_sensitive_fields:
            return encrypted_extra

        try:
            config = json.loads(encrypted_extra)
        except (TypeError, json.JSONDecodeError):
            return encrypted_extra

        masked_encrypted_extra = redact_sensitive(
            config,
            cls.encrypted_extra_sensitive_fields,
        )

        return json.dumps(masked_encrypted_extra)

    @classmethod
    def unmask_encrypted_extra(cls, old: str | None, new: str | None) -> str | None:
        """
        Remove masks from `encrypted_extra`.

        This method allows reusing existing values from the current encrypted extra on
        updates. It's useful for reusing masked passwords, allowing keys to be updated
        without having to provide sensitive data to the client.
        """
        if old is None or new is None:
            return new

        try:
            old_config = json.loads(old)
            new_config = json.loads(new)
        except (TypeError, json.JSONDecodeError):
            return new

        new_config = reveal_sensitive(
            old_config,
            new_config,
            cls.encrypted_extra_sensitive_fields,
        )

        return json.dumps(new_config)

    @classmethod
    def get_public_information(cls) -> dict[str, Any]:
        """
        Construct a Dict with properties we want to expose.

        :returns: Dict with properties of our class like supports_file_upload
        and disable_ssh_tunneling
        """
        return {
            "supports_file_upload": cls.supports_file_upload,
            "disable_ssh_tunneling": cls.disable_ssh_tunneling,
            "supports_dynamic_catalog": cls.supports_dynamic_catalog,
            "supports_oauth2": cls.supports_oauth2,
        }

    @classmethod
    def validate_database_uri(cls, sqlalchemy_uri: URL) -> None:
        """
        Validates a database SQLAlchemy URI per engine spec.
        Use this to implement a final validation for unwanted connection configuration

        :param sqlalchemy_uri:
        """
        if db_engine_uri_validator := app.config["DB_SQLA_URI_VALIDATOR"]:
            db_engine_uri_validator(sqlalchemy_uri)

        if existing_disallowed := cls.disallow_uri_query_params.get(
            sqlalchemy_uri.get_driver_name(), set()
        ).intersection(sqlalchemy_uri.query):
            raise ValueError(f"Forbidden query parameter(s): {existing_disallowed}")

    @classmethod
    def denormalize_name(cls, dialect: Dialect, name: str) -> str:
        if (
            hasattr(dialect, "requires_name_normalize")
            and dialect.requires_name_normalize
        ):
            return dialect.denormalize_name(name)

        return name

    @classmethod
    def quote_table(cls, table: Table, dialect: Dialect) -> str:
        """
        Fully quote a table name, including the schema and catalog.
        """
        quoters = {
            "catalog": dialect.identifier_preparer.quote_schema,
            "schema": dialect.identifier_preparer.quote_schema,
            "table": dialect.identifier_preparer.quote,
        }

        return ".".join(
            function(getattr(table, key))
            for key, function in quoters.items()
            if getattr(table, key)
        )


# schema for adding a database by providing parameters instead of the
# full SQLAlchemy URI
class BasicParametersSchema(Schema):
    username = fields.String(
        required=True, allow_none=True, metadata={"description": __("Username")}
    )
    password = fields.String(allow_none=True, metadata={"description": __("Password")})
    host = fields.String(
        required=True, metadata={"description": __("Hostname or IP address")}
    )
    port = fields.Integer(
        required=True,
        metadata={"description": __("Database port")},
        validate=Range(min=0, max=2**16, max_inclusive=False),
    )
    database = fields.String(
        required=True, metadata={"description": __("Database name")}
    )
    query = fields.Dict(
        keys=fields.Str(),
        values=fields.Raw(),
        metadata={"description": __("Additional parameters")},
    )
    encryption = fields.Boolean(
        required=False,
        metadata={"description": __("Use an encrypted connection to the database")},
    )
    ssh = fields.Boolean(
        required=False,
        metadata={"description": __("Use an ssh tunnel connection to the database")},
    )


class BasicParametersType(TypedDict, total=False):
    username: str | None
    password: str | None
    host: str
    port: int
    database: str
    query: dict[str, Any]
    encryption: bool


class BasicPropertiesType(TypedDict):
    parameters: BasicParametersType


class BasicParametersMixin:
    """
    Mixin for configuring DB engine specs via a dictionary.

    With this mixin the SQLAlchemy engine can be configured through
    individual parameters, instead of the full SQLAlchemy URI. This
    mixin is for the most common pattern of URI:

        engine+driver://user:password@host:port/dbname[?key=value&key=value...]

    """

    # schema describing the parameters used to configure the DB
    parameters_schema = BasicParametersSchema()

    # recommended driver name for the DB engine spec
    default_driver = ""

    # query parameter to enable encryption in the database connection
    # for Postgres this would be `{"sslmode": "verify-ca"}`, eg.
    encryption_parameters: dict[str, str] = {}

    @classmethod
    def build_sqlalchemy_uri(  # pylint: disable=unused-argument
        cls,
        parameters: BasicParametersType,
        encrypted_extra: dict[str, str] | None = None,
    ) -> str:
        # TODO (betodealmeida): this method should also build `connect_args`
        # make a copy so that we don't update the original
        query = parameters.get("query", {}).copy()
        if parameters.get("encryption"):
            if not cls.encryption_parameters:
                raise Exception(  # pylint: disable=broad-exception-raised
                    "Unable to build a URL with encryption enabled"
                )
            query.update(cls.encryption_parameters)

        return str(
            URL.create(
                f"{cls.engine}+{cls.default_driver}".rstrip("+"),  # type: ignore
                username=parameters.get("username"),
                password=parameters.get("password"),
                host=parameters["host"],
                port=parameters["port"],
                database=parameters["database"],
                query=query,
            )
        )

    @classmethod
    def get_parameters_from_uri(  # pylint: disable=unused-argument
        cls, uri: str, encrypted_extra: dict[str, Any] | None = None
    ) -> BasicParametersType:
        url = make_url_safe(uri)
        query = {
            key: value
            for (key, value) in url.query.items()
            if (key, value) not in cls.encryption_parameters.items()
        }
        encryption = all(
            item in url.query.items() for item in cls.encryption_parameters.items()
        )
        return {
            "username": url.username,
            "password": url.password,
            "host": url.host,
            "port": url.port,
            "database": url.database,
            "query": query,
            "encryption": encryption,
        }

    @classmethod
    def validate_parameters(
        cls, properties: BasicPropertiesType
    ) -> list[SupersetError]:
        """
        Validates any number of parameters, for progressive validation.

        If only the hostname is present it will check if the name is resolvable. As more
        parameters are present in the request, more validation is done.
        """
        errors: list[SupersetError] = []

        required = {"host", "port", "username", "database"}
        parameters = properties.get("parameters", {})
        present = {key for key in parameters if parameters.get(key, ())}

        if missing := sorted(required - present):
            errors.append(
                SupersetError(
                    message=f"One or more parameters are missing: {', '.join(missing)}",
                    error_type=SupersetErrorType.CONNECTION_MISSING_PARAMETERS_ERROR,
                    level=ErrorLevel.WARNING,
                    extra={"missing": missing},
                ),
            )

        host = parameters.get("host", None)
        if not host:
            return errors
        if not is_hostname_valid(host):
            errors.append(
                SupersetError(
                    message="The hostname provided can't be resolved.",
                    error_type=SupersetErrorType.CONNECTION_INVALID_HOSTNAME_ERROR,
                    level=ErrorLevel.ERROR,
                    extra={"invalid": ["host"]},
                ),
            )
            return errors

        port = parameters.get("port", None)
        if not port:
            return errors
        try:
            port = int(port)
        except (ValueError, TypeError):
            errors.append(
                SupersetError(
                    message="Port must be a valid integer.",
                    error_type=SupersetErrorType.CONNECTION_INVALID_PORT_ERROR,
                    level=ErrorLevel.ERROR,
                    extra={"invalid": ["port"]},
                ),
            )
        if not (isinstance(port, int) and 0 <= port < 2**16):
            errors.append(
                SupersetError(
                    message=(
                        "The port must be an integer between 0 and 65535 (inclusive)."
                    ),
                    error_type=SupersetErrorType.CONNECTION_INVALID_PORT_ERROR,
                    level=ErrorLevel.ERROR,
                    extra={"invalid": ["port"]},
                ),
            )
        elif not is_port_open(host, port):
            errors.append(
                SupersetError(
                    message="The port is closed.",
                    error_type=SupersetErrorType.CONNECTION_PORT_CLOSED_ERROR,
                    level=ErrorLevel.ERROR,
                    extra={"invalid": ["port"]},
                ),
            )

        return errors

    @classmethod
    def parameters_json_schema(cls) -> Any:
        """
        Return configuration parameters as OpenAPI.
        """
        if not cls.parameters_schema:
            return None

        spec = APISpec(
            title="Database Parameters",
            version="1.0.0",
            openapi_version="3.0.2",
            plugins=[MarshmallowPlugin()],
        )
        spec.components.schema(cls.__name__, schema=cls.parameters_schema)
        return spec.to_dict()["components"]["schemas"][cls.__name__]
