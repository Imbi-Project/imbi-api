from __future__ import annotations

import re
import typing

import jsonpatch
import jsonpointer
import psycopg2.errors
import psycopg2.errorcodes
import pydantic

from imbi import errors
from imbi.endpoints import base
from imbi.endpoints.components import models, scoring


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

    def get_pagination_token_from_request(self) -> models.ComponentToken:
        return models.ComponentToken.from_request(self.request)

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
        result = await self.postgres_execute(
            self.GET_SQL, {'package_url': kwargs['package_url']},
            f'get-{self.NAME}')
        if not result:
            raise errors.ItemNotFound(instance=self.request.uri)

        original = models.Component.model_validate(result.row)
        original_dict = original.model_dump()
        try:
            patch = jsonpatch.JsonPatch(self.get_request_body())
        except (jsonpatch.JsonPatchException,
                jsonpointer.JsonPointerException) as error:
            raise errors.BadJsonPatch(error)
        updated_dict = patch.apply(original_dict)
        if all(original_dict[k] == updated_dict[k] for k in updated_dict):
            self._add_self_link(self.request.path)
            self._add_link_header()
            self.set_status(304)
            return

        try:
            updated = models.Component.model_validate(updated_dict)
        except pydantic.ValidationError as error:
            all_errors = error.errors(include_context=False)
            raise errors.BadRequest(
                'failed to validate patched version: %s',
                str(error).replace('\n', ';'),
                title='Invalid Component generated by update',
                detail=all_errors[0]['msg'],
                validation_errors=all_errors) from None

        updated_dict.update({
            'current_package_url': original.package_url,
            'username': self._current_user.username,
        })
        result = await self.postgres_execute(self.PATCH_SQL, updated_dict,
                                             f'patch-{self.NAME}')
        if not result:
            raise errors.DatabaseError('No rows were returned from PATCH_SQL',
                                       title='failed to update record')

        if self._project_update_required(original, updated):
            tags = {
                'key': 'bulk_update',
                'operation': 'component_score',
                'endpoint': self.NAME
            }
            async with self.application.stats.track_duration(tags):
                await self._update_affected_projects(updated.package_url)

        await self._get({'package_url': updated.package_url})

    def _project_update_required(self, original: models.Component,
                                 updated: models.Component) -> bool:
        # only update if we have a fact id to worry about
        config = self.settings.get('components', {}) or {}
        if config.get('project_fact_type_id') is not None:
            # status change will always affect component scores
            if updated.status != original.status:
                return True
            # if the final status is active, then we only care if the
            # active version has changed
            if (updated.status == models.ComponentStatus.ACTIVE
                    and updated.active_version != original.active_version):
                return True
        return False

    async def _update_affected_projects(self, package_url: str) -> None:
        # the following is used to propagate foreign key violations
        # to our code ... these can happen if a project is removed
        # between the time that we retrieve the affected projects
        # and when we upsert the project fact
        def on_postgres_error(metric_name: str,
                              exc: Exception) -> typing.Optional[Exception]:
            if isinstance(exc, psycopg2.errors.ForeignKeyViolation):
                raise exc
            else:
                return self.on_postgres_error(metric_name, exc)

        result = await self.postgres_execute(
            'SELECT DISTINCT project_id'
            '  FROM v1.project_components'
            ' WHERE package_url = %(package_url)s',
            {'package_url': package_url},
            metric_name='retrieve-affected-projects')
        if result:
            project_ids = {row['project_id'] for row in result}
            self.logger.info('found %s projects affected by this change',
                             len(project_ids))
            async with self.application.postgres_connector(
                    on_postgres_error, self.on_postgres_timing) as connector:
                for project_id in project_ids:
                    try:
                        await scoring.update_component_score_for_project(
                            project_id, connector, self.application)
                    except psycopg2.errors.ForeignKeyViolation as exc:
                        self.logger.warning(
                            'failed to update component score for project'
                            ' id %s: %s', project_id, exc)


class ProjectComponentsRequestHandler(base.PaginatedCollectionHandler):
    # the status & score columns will become "real" when we figure
    # out how we want to score these in the future
    COLLECTION_SQL = re.sub(
        r'\s+', ' ', """\
        SELECT c.package_url, c."name", c.icon_class, c.status,
               c.active_version, v.version
          FROM v1.project_components AS p
          JOIN v1.component_versions AS v ON v.id = p.version_id
          JOIN v1.components AS c ON c.package_url = v.package_url
         WHERE c.package_url > %(starting_package)s
           AND p.project_id = %(project_id)s
         ORDER BY c.package_url ASC
        """)

    def _postprocess_item(self, item) -> None:
        """Convert the database item into what the API exposes

        Initially, ``item['status']`` is the *component's* status which
        match the *project component's* status for everything except for
        ``Active``. The project component status for active components
        is either ``Unscored``, ``Up-to-date``, or ``Outdated`` based
        on the component's active version.

        We also want to add a link to the component details.

        """
        item['link'] = self.reverse_url('component', item['package_url'])
        project_component = scoring.ProjectComponentRow.model_validate(item)
        if project_component.active_version is None:
            item['status'] = models.ProjectComponentStatus.UNSCORED
        elif project_component.status == models.ComponentStatus.ACTIVE:
            if project_component.version in project_component.active_version:
                item['status'] = models.ProjectComponentStatus.UP_TO_DATE
            else:
                item['status'] = models.ProjectComponentStatus.OUTDATED

    def get_pagination_token_from_request(
            self, *, project_id: str) -> models.ProjectComponentsToken:
        return models.ProjectComponentsToken.from_request(
            self.request, project_id=project_id)
