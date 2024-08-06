from __future__ import annotations

import enum
import re

import pydantic
import typing_extensions as typing

from imbi import errors, semver
from imbi.endpoints import base


class ComponentStatus(str, enum.Enum):
    ACTIVE = 'Active'
    DEPRECATED = 'Deprecated'
    FORBIDDEN = 'Forbidden'


class Component(pydantic.BaseModel):
    package_url: str = pydantic.constr(pattern=r'^pkg:')
    name: str
    status: ComponentStatus
    icon_class: str
    active_version: typing.Union[semver.VersionRange, None]
    home_page: typing.Union[str, None]


class ComponentToken(base.PaginationToken):
    """Pagination token that includes the starting package URL"""
    def __init__(self, *, starting_package: str = '', **kwargs) -> None:
        super().__init__(starting_package=starting_package, **kwargs)

    def with_first(self, value: dict[str, object]) -> typing.Self:
        kwargs = self.as_dict(starting_package=value['package_url'])
        return ComponentToken(**kwargs)


class CollectionRequestHandler(base.PaginatedCollectionHandler):
    NAME = 'components'
    ITEM_NAME = 'component'
    ID_KEY = 'package_url'
    FIELDS = [
        'package_url', 'name', 'status', 'home_page', 'icon_class',
        'active_version'
    ]
    DEFAULTS = {
        'status': 'Active',
        'icon_class': 'fas fa-save',
        'active_version': None
    }

    COLLECTION_SQL = re.sub(
        r'\s+', ' ', """\
        SELECT c.package_url, c."name", c.status, c.home_page, c.icon_class,
               c.active_version, COUNT(v.id) AS version_count,
               COUNT(p.project_id) AS project_count, c.created_at,
               c.created_by, c.last_modified_at, c.last_modified_by
          FROM v1.components AS c
          LEFT JOIN v1.component_versions AS v ON v.package_url = c.package_url
          LEFT JOIN v1.project_components AS p ON p.version_id = v.id
         WHERE c.package_url > %(starting_package)s
         GROUP BY c.package_url, c."name", c.status, c.home_page, c.icon_class,
                  c.active_version, c.created_at, c.created_by,
                  c.last_modified_at, c.last_modified_by
         ORDER BY c.package_url ASC
         LIMIT %(limit)s
        """)
    GET_SQL = re.sub(
        r'\s+', ' ', """\
        SELECT package_url, "name", status, home_page, icon_class,
               active_version, created_at, created_by,
               last_modified_at, last_modified_by
          FROM v1.components
         WHERE package_url = %(package_url)s
        """)
    POST_SQL = re.sub(
        r'\s+', ' ', """\
        INSERT INTO v1.components
                    (package_url, "name", status, home_page,
                     active_version, icon_class, created_by)
             VALUES (%(package_url)s, %(name)s, %(status)s, %(home_page)s,
                     %(active_version)s, %(icon_class)s, %(username)s)
          RETURNING *
        """)

    def get_pagination_token_from_request(self) -> ComponentToken:
        return ComponentToken.from_request(self.request)

    @base.require_permission('admin')
    async def post(self, *args, **kwargs) -> None:
        await super().post(*args, **kwargs)


class RecordRequestHandler(base.CRUDRequestHandler):
    NAME = 'component'
    ID_KEY = 'package_url'
    GET_SQL = re.sub(
        r'\s+', ' ', """\
        SELECT package_url, "name", status, home_page, icon_class,
               active_version, created_at, created_by,
               last_modified_at, last_modified_by
          FROM v1.components
         WHERE package_url = %(package_url)s
        """)
    DELETE_SQL = re.sub(
        r'\s+', ' ', """\
        DELETE FROM v1.components WHERE package_url = %(package_url)s
        """)
    PATCH_SQL = re.sub(
        r'\s+', ' ', """\
        UPDATE v1.components
           SET package_url = %(package_url)s,
               "name" = %(name)s,
               status = %(status)s,
               home_page = %(home_page)s,
               icon_class = %(icon_class)s,
               active_version = %(active_version)s,
               last_modified_at = CURRENT_TIMESTAMP,
               last_modified_by = %(username)s
         WHERE package_url = %(current_package_url)s
        """)

    @base.require_permission('admin')
    async def delete(self, *args, **kwargs):
        await super().delete(*args, **kwargs)

    @base.require_permission('admin')
    async def patch(self, *args, **kwargs):
        await super().patch(*args, **kwargs)

    def _check_validity(self, instance: dict[str, typing.Any]) -> bool:
        try:
            Component.model_validate(instance)
        except pydantic.ValidationError as error:
            all_errors = error.errors(include_context=False)
            raise errors.BadRequest(
                'failed to validate patched version: %s',
                str(error).replace('\n', ';'),
                title='Invalid Component generated by update',
                detail=all_errors[0]['msg'],
                validation_errors=all_errors) from None
        else:
            return True


class ProjectComponentsToken(base.PaginationToken):
    """Pagination token that includes the starting package URL and project"""
    def __init__(self,
                 *,
                 starting_package: str = '',
                 project_id: int | str,
                 **kwargs) -> None:
        try:
            project_id = int(project_id)
        except ValueError:
            raise errors.BadRequest('Invalid project id %r, expected integer',
                                    project_id)
        super().__init__(starting_package=starting_package,
                         project_id=project_id,
                         **kwargs)

    def with_first(self, value: dict[str, object]) -> typing.Self:
        kwargs = self.as_dict(starting_package=value['package_url'])
        return ProjectComponentsToken(**kwargs)


class ProjectComponentsRequestHandler(base.PaginatedCollectionHandler):
    # the status & score columns will become "real" when we figure
    # out how we want to score these in the future
    COLLECTION_SQL = re.sub(
        r'\s+', ' ', """\
        SELECT c.package_url, c."name", v.version, c.icon_class,
               c.status, NULL AS score
          FROM v1.project_components AS p
          JOIN v1.component_versions AS v ON v.id = p.version_id
          JOIN v1.components AS c ON c.package_url = v.package_url
         WHERE c.package_url > %(starting_package)s
           AND p.project_id = %(project_id)s
         ORDER BY c.package_url ASC
        """)

    def get_pagination_token_from_request(
            self, *, project_id: str) -> ProjectComponentsToken:
        return ProjectComponentsToken.from_request(self.request,
                                                   project_id=project_id)
