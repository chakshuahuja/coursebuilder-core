# Copyright 2012 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Classes and methods to create and manage Gatherings."""

__author__ = 'Saifu Angto (saifu@google.com)'


import cgi
import collections
import datetime
import os
import urllib

import jinja2

import appengine_config
from common import crypto
from common import tags
from common import utils as common_utils
from common import schema_fields
from common import resource
from common import utc
from controllers import sites
from controllers import utils
from models import resources_display
from models import courses
from models import custom_modules
from models import entities
from models import models
from models import roles
from models.student_work import KeyProperty
from models import transforms
from modules.gatherings import messages
from modules.dashboard import dashboard
from modules.i18n_dashboard import i18n_dashboard
from modules.news import news
from modules.oeditor import oeditor

from google.appengine.ext import db

MODULE_NAME = 'gatherings'
MODULE_TITLE = 'Gatherings'
TEMPLATE_DIR = os.path.join(
    appengine_config.BUNDLE_ROOT, 'modules', MODULE_NAME, 'templates')


class GatheringsRights(object):
    """Manages view/edit rights for gatherings."""

    @classmethod
    def can_view(cls, unused_handler):
        return True

    @classmethod
    def can_edit(cls, handler):
        return roles.Roles.is_course_admin(handler.app_context)

    @classmethod
    def can_delete(cls, handler):
        return cls.can_edit(handler)

    @classmethod
    def can_add(cls, handler):
        return cls.can_edit(handler)

    @classmethod
    def apply_rights(cls, handler, items):
        """Filter out items that current user can't see."""
        if GatheringsRights.can_edit(handler):
            return items

        allowed = []
        for item in items:
            if not item.is_draft:
                allowed.append(item)

        return allowed


class GatheringsHandlerMixin(object):
    def get_gathering_action_url(self, action, key=None):
        args = {'action': action}
        if key:
            args['key'] = key
        return self.canonicalize_url(
            '{}?{}'.format(
                GatheringsDashboardHandler.URL, urllib.urlencode(args)))

    def format_items_for_template(self, items, user=None):
        """Formats a list of entities into template values."""
        template_items = []

        joined_gatherings = {}
        if user:
            joined_gatherings = {
                gu.gathering
                for gu in
                    GatheringsUsersEntity
                    .all()
                    .filter('user =', user.user_id())
                    .filter('gathering IN', [i.key() for i in items])
            }
        for item_entitiy in items:
            item = transforms.entity_to_dict(item_entitiy)
            if user:
                item['joined'] = item_entitiy.key() in joined_gatherings
            # add 'edit' actions

            if GatheringsRights.can_edit(self):
                item['edit_action'] = self.get_gathering_action_url(
                    GatheringsDashboardHandler.EDIT_ACTION, key=item['key'])

                item['delete_xsrf_token'] = self.create_xsrf_token(
                    GatheringsDashboardHandler.DELETE_ACTION)
                item['delete_action'] = self.get_gathering_action_url(
                    GatheringsDashboardHandler.DELETE_ACTION,
                    key=item['key'])

            template_items.append(item)

        output = {}
        output['children'] = template_items

        # add 'add' action
        if GatheringsRights.can_edit(self):
            output['add_xsrf_token'] = self.create_xsrf_token(
                GatheringsDashboardHandler.ADD_ACTION)
            output['add_action'] = self.get_gathering_action_url(
                GatheringsDashboardHandler.ADD_ACTION)

        return output


class GatheringsStudentHandler(
        GatheringsHandlerMixin, utils.BaseHandler,
        utils.ReflectiveRequestHandler):
    URL = '/gatherings'
    default_action = 'list'
    get_actions = [default_action]
    post_actions = []

    def get_list(self):
        """Shows a list of gatherings."""
        student = None
        user = self.personalize_page_and_get_user()
        transient_student = False
        if user is None:
            transient_student = True
        else:
            student = models.Student.get_enrolled_student_by_user(user)
            if not student:
                transient_student = True
        self.template_value['transient_student'] = transient_student
        locale = self.app_context.get_current_locale()
        if locale == self.app_context.default_locale:
            locale = None
        items = GatheringEntity.get_gatherings(locale=locale)
        items = GatheringsRights.apply_rights(self, items)
        self.template_value['gatherings'] = self.format_items_for_template(
            items,
            self.get_user(),
        )
        self._render()

    def _render(self):
        self.template_value['navbar'] = {'gatherings': True}
        self.render('gatherings.html')


class GatheringsDashboardHandler(
        GatheringsHandlerMixin, dashboard.DashboardHandler):
    """Handler for gatherings."""

    LIST_ACTION = 'edit_gatherings'
    EDIT_ACTION = 'edit_gathering'
    DELETE_ACTION = 'delete_gathering'
    ADD_ACTION = 'add_gathering'
    DEFAULT_TITLE_TEXT = 'New Gathering'

    get_actions = [LIST_ACTION, EDIT_ACTION]
    post_actions = [ADD_ACTION, DELETE_ACTION]

    LINK_URL = 'edit_gatherings'
    URL = '/{}'.format(LINK_URL)
    LIST_URL = '{}?action={}'.format(LINK_URL, LIST_ACTION)

    @classmethod
    def get_child_routes(cls):
        """Add child handlers for REST."""
        return [
            (GatheringsItemRESTHandler.URL, GatheringsItemRESTHandler)]

    def get_edit_gatherings(self):
        """Shows a list of gatherings."""
        items = GatheringEntity.get_gatherings()
        items = GatheringsRights.apply_rights(self, items)

        main_content = self.get_template(
            'gathering_list.html', [TEMPLATE_DIR]).render({
                'gatherings': self.format_items_for_template(items),
                'status_xsrf_token': self.create_xsrf_token(
                    GatheringsItemRESTHandler.STATUS_ACTION)
            })

        self.render_page({
            'page_title': self.format_title('Gatherings'),
            'main_content': jinja2.utils.Markup(main_content)})

    def get_edit_gathering(self):
        """Shows an editor for an gathering."""

        key = self.request.get('key')

        schema = GatheringsItemRESTHandler.SCHEMA()

        exit_url = self.canonicalize_url('/{}'.format(self.LIST_URL))
        rest_url = self.canonicalize_url('/rest/gatherings/item')
        form_html = oeditor.ObjectEditor.get_html_for(
            self,
            schema.get_json_schema(),
            schema.get_schema_dict(),
            key, rest_url, exit_url,
            delete_method='delete',
            delete_message='Are you sure you want to delete this gathering?',
            delete_url=self._get_delete_url(
                GatheringsItemRESTHandler.URL, key, 'gathering-delete'),
            display_types=schema.get_display_types())

        self.render_page({
            'main_content': form_html,
            'page_title': 'Edit Gatherings',
        }, in_action=self.LIST_ACTION)

    def _get_delete_url(self, base_url, key, xsrf_token_name):
        return '%s?%s' % (
            self.canonicalize_url(base_url),
            urllib.urlencode({
                'key': key,
                'xsrf_token': cgi.escape(
                    self.create_xsrf_token(xsrf_token_name)),
            }))

    def post_delete_gathering(self):
        """Deletes an gathering."""
        if not GatheringsRights.can_delete(self):
            self.error(401)
            return

        key = self.request.get('key')
        entity = GatheringEntity.get(key)
        if entity:
            entity.delete()
        self.redirect('/{}'.format(self.LIST_URL))

    def post_add_gathering(self):
        """Adds a new gathering and redirects to an editor for it."""
        if not GatheringsRights.can_add(self):
            self.error(401)
            return

        entity = GatheringEntity.make(self.DEFAULT_TITLE_TEXT, '', True)
        entity.put()

        self.redirect(self.get_gathering_action_url(
            self.EDIT_ACTION, key=entity.key()))


class GatheringsItemRESTHandler(utils.BaseRESTHandler):
    """Provides REST API for an gathering."""

    URL = '/rest/gatherings/item'

    ACTION = 'gathering-put'
    STATUS_ACTION = 'set_draft_status_gathering'

    @classmethod
    def SCHEMA(cls):
        schema = schema_fields.FieldRegistry('Gathering',
            extra_schema_dict_values={
                'className': 'inputEx-Group new-form-layout'})
        schema.add_property(schema_fields.SchemaField(
            'key', 'ID', 'string', editable=False, hidden=True))
        schema.add_property(schema_fields.SchemaField(
            'title', 'Title', 'string',
            description=messages.GATHERING_TITLE_DESCRIPTION))
        schema.add_property(schema_fields.SchemaField(
            'html', 'Body', 'html',
            description=messages.GATHERING_BODY_DESCRIPTION,
            extra_schema_dict_values={
                'supportCustomTags': tags.CAN_USE_DYNAMIC_TAGS.value,
                'excludedCustomTags': tags.EditorBlacklists.COURSE_SCOPE},
            optional=True))
        schema.add_property(schema_fields.SchemaField(
            'start_time', 'Start Time', 'datetime',
            description=messages.GATHERING_DATE_DESCRIPTION,
            extra_schema_dict_values={
                '_type': 'datetime',
                'className': 'inputEx-CombineField gcb-datetime '
                'inputEx-fieldWrapper inputEx-required'}))
        schema.add_property(schema_fields.SchemaField(
            'end_time', 'End Time', 'datetime',
            description=messages.GATHERING_DATE_DESCRIPTION,
            extra_schema_dict_values={
                '_type': 'datetime',
                'className': 'inputEx-CombineField gcb-datetime '
                'inputEx-fieldWrapper inputEx-required'}))
        schema.add_property(schema_fields.SchemaField(
            'is_draft', 'Status', 'boolean',
            description=messages.GATHERING_STATUS_DESCRIPTION,
            extra_schema_dict_values={'className': 'split-from-main-group'},
            optional=True,
            select_data=[
                (True, resources_display.DRAFT_TEXT),
                (False, resources_display.PUBLISHED_TEXT)]))
        return schema

    def get(self):
        """Handles REST GET verb and returns an object as JSON payload."""
        key = self.request.get('key')

        try:
            entity = GatheringEntity.get(key)
        except db.BadKeyError:
            entity = None

        if not entity:
            transforms.send_json_response(
                self, 404, 'Object not found.', {'key': key})
            return
        viewable = GatheringsRights.apply_rights(self, [entity])
        if not viewable:
            transforms.send_json_response(
                self, 401, 'Access denied.', {'key': key})
            return
        entity = viewable[0]

        schema = GatheringsItemRESTHandler.SCHEMA()

        entity_dict = transforms.entity_to_dict(entity)

        json_payload = transforms.dict_to_json(entity_dict)
        transforms.send_json_response(
            self, 200, 'Success.',
            payload_dict=json_payload,
            xsrf_token=crypto.XsrfTokenManager.create_xsrf_token(self.ACTION))

    def put(self):
        """Handles REST PUT verb with JSON payload."""
        request = transforms.loads(self.request.get('request'))
        key = request.get('key')

        if not self.assert_xsrf_token_or_fail(
                request, self.ACTION, {'key': key}):
            return

        if not GatheringsRights.can_edit(self):
            transforms.send_json_response(
                self, 401, 'Access denied.', {'key': key})
            return

        entity = GatheringEntity.get(key)
        if not entity:
            transforms.send_json_response(
                self, 404, 'Object not found.', {'key': key})
            return

        schema = GatheringsItemRESTHandler.SCHEMA()

        payload = request.get('payload')
        update_dict = transforms.json_to_dict(
            transforms.loads(payload), schema.get_json_schema_dict())
        if entity.is_draft and not update_dict.get('set_draft'):
            item = news.NewsItem(
                str(TranslatableResourceGathering.key_for_entity(entity)),
                GatheringsStudentHandler.URL.lstrip('/'))
            news.CourseNewsDao.add_news_item(item)

        del update_dict['key']  # Don't overwrite key member method in entity.
        transforms.dict_to_entity(entity, update_dict)

        entity.put()

        transforms.send_json_response(self, 200, 'Saved.')

    def delete(self):
        """Deletes an gathering."""
        key = self.request.get('key')

        if not self.assert_xsrf_token_or_fail(
                self.request, 'gathering-delete', {'key': key}):
            return

        if not GatheringsRights.can_delete(self):
            self.error(401)
            return

        entity = GatheringEntity.get(key)
        if not entity:
            transforms.send_json_response(
                self, 404, 'Object not found.', {'key': key})
            return

        entity.delete()

        transforms.send_json_response(self, 200, 'Deleted.')

    @classmethod
    def post_set_draft_status(cls, handler):
        """Sets the draft status of a course component.

        Only works with CourseModel13 courses, but the REST handler
        is only called with this type of courses.

        XSRF is checked in the dashboard.
        """
        key = handler.request.get('key')

        if not GatheringsRights.can_edit(handler):
            transforms.send_json_response(
                handler, 401, 'Access denied.', {'key': key})
            return

        entity = GatheringEntity.get(key)
        if not entity:
            transforms.send_json_response(
                handler, 404, 'Object not found.', {'key': key})
            return

        set_draft = handler.request.get('set_draft')
        if set_draft == '1':
            set_draft = True
        elif set_draft == '0':
            set_draft = False
        else:
            transforms.send_json_response(
                handler, 401, 'Invalid set_draft value, expected 0 or 1.',
                {'set_draft': set_draft}
            )
            return

        if entity.is_draft and not set_draft:
            item = news.NewsItem(
                str(TranslatableResourceGathering.key_for_entity(entity)),
                GatheringsStudentHandler.URL.lstrip('/'))
            news.CourseNewsDao.add_news_item(item)

        entity.is_draft = set_draft
        entity.put()

        transforms.send_json_response(
            handler,
            200,
            'Draft status set to %s.' % (
                resources_display.DRAFT_TEXT if set_draft else
                resources_display.PUBLISHED_TEXT
            ), {
                'is_draft': set_draft
            }
        )
        return


class GatheringEntity(entities.BaseEntity):
    """A class that represents a persistent database entity of gatherings.

    Note that this class was added to Course Builder prior to the idioms
    introduced in models.models.BaseJsonDao and friends.  That being the
    case, this class is much more hand-coded and not well integrated into
    the structure of callbacks and hooks that have accumulated around
    entity caching, i18n, and the like.
    """

    title = db.StringProperty(indexed=False)
    start_time = db.DateTimeProperty()
    end_time = db.DateTimeProperty()
    html = db.TextProperty(indexed=False)
    is_draft = db.BooleanProperty()

    _MEMCACHE_KEY = 'gatherings'

    @classmethod
    def get_gatherings(cls, locale=None):
        memcache_key = cls._cache_key(locale)
        items = models.MemcacheManager.get(memcache_key)
        if items is None:
            items = list(common_utils.iter_all(GatheringEntity.all()))
            items.sort(key=lambda item: item.start_time, reverse=True)
            if locale:
                cls._translate_content(items)

            # TODO(psimakov): prepare to exceed 1MB max item size
            # read more here: http://stackoverflow.com
            #   /questions/5081502/memcache-1-mb-limit-in-google-app-engine
            models.MemcacheManager.set(memcache_key, items)
        return items

    @classmethod
    def _cache_key(cls, locale=None):
        if not locale:
            return cls._MEMCACHE_KEY
        return cls._MEMCACHE_KEY + ':' + locale

    @classmethod
    def purge_cache(cls, locale=None):
        models.MemcacheManager.delete(cls._cache_key(locale))

    @classmethod
    def make(cls, title, html, is_draft):
        entity = cls()
        entity.title = title
        entity.start_time = utc.now_as_datetime()
        entity.end_time = entitity.start_time + datetime.timedelta(minutes=30)
        entity.html = html
        entity.is_draft = is_draft
        return entity

    def put(self):
        """Do the normal put() and also invalidate memcache."""
        result = super(GatheringEntity, self).put()
        self.purge_cache()
        if i18n_dashboard.I18nProgressDeferredUpdater.is_translatable_course():
            i18n_dashboard.I18nProgressDeferredUpdater.update_resource_list(
                [TranslatableResourceGathering.key_for_entity(self)])
        return result

    def delete(self):
        """Do the normal delete() and invalidate memcache."""
        news.CourseNewsDao.remove_news_item(
            str(TranslatableResourceGathering.key_for_entity(self)))
        super(GatheringEntity, self).delete()
        self.purge_cache()

    @classmethod
    def _translate_content(cls, items):
        app_context = sites.get_course_for_current_request()
        course = courses.Course.get(app_context)
        key_list = [
            TranslatableResourceGathering.key_for_entity(item)
            for item in items]
        FakeDto = collections.namedtuple('FakeDto', ['dict'])
        fake_items = [
            FakeDto({'title': item.title, 'html': item.html})
            for item in items]
        i18n_dashboard.translate_dto_list(course, fake_items, key_list)
        for item, fake_item in zip(items, fake_items):
            item.title = str(fake_item.dict['title'])
            item.html = str(fake_item.dict['html'])


class GatheringsUsersEntity(entities.BaseEntity):
    gathering = KeyProperty(kind=GatheringEntity.kind())
    user = db.StringProperty()

class TranslatableResourceGathering(
    i18n_dashboard.AbstractTranslatableResourceType):

    @classmethod
    def get_ordering(cls):
        return i18n_dashboard.TranslatableResourceRegistry.ORDERING_LAST

    @classmethod
    def get_title(cls):
        return MODULE_TITLE

    @classmethod
    def key_for_entity(cls, gathering, course=None):
        return resource.Key(ResourceHandlerGathering.TYPE,
                            gathering.key().id(), course)

    @classmethod
    def get_resources_and_keys(cls, course):
        return [(gathering, cls.key_for_entity(gathering, course))
                for gathering in GatheringEntity.get_gatherings()]

    @classmethod
    def get_resource_types(cls):
        return [ResourceHandlerGathering.TYPE]

    @classmethod
    def notify_translations_changed(cls, resource_bundle_key):
        GatheringEntity.purge_cache(resource_bundle_key.locale)

    @classmethod
    def get_i18n_title(cls, resource_key):
        locale = None
        app_context = sites.get_course_for_current_request()
        if (app_context and
            app_context.default_locale != app_context.get_current_locale()):
            locale = app_context.get_current_locale()
        gatherings = GatheringEntity.get_gatherings(locale)
        item = common_utils.find(
            lambda a: a.key().id() == int(resource_key.key), gatherings)
        return item.title if item else None


class ResourceHandlerGathering(resource.AbstractResourceHandler):
    """Generic resoruce accessor for applying translations to gatherings."""

    TYPE = 'gathering'

    @classmethod
    def _entity_key(cls, key):
        return db.Key.from_path(GatheringEntity.kind(), int(key))

    @classmethod
    def get_resource(cls, course, key):
        return GatheringEntity.get(cls._entity_key(key))

    @classmethod
    def get_resource_title(cls, rsrc):
        return rsrc.title

    @classmethod
    def get_schema(cls, course, key):
        return GatheringsItemRESTHandler.SCHEMA()

    @classmethod
    def get_data_dict(cls, course, key):
        entity = cls.get_resource(course, key)
        return transforms.entity_to_dict(entity)

    @classmethod
    def get_view_url(cls, rsrc):
        return GatheringsStudentHandler.URL.lstrip('/')

    @classmethod
    def get_edit_url(cls, key):
        return (GatheringsDashboardHandler.LINK_URL + '?' +
                urllib.urlencode({
                    'action': GatheringsDashboardHandler.EDIT_ACTION,
                    'key': cls._entity_key(key),
                }))


custom_module = None


def on_module_enabled():
    resource.Registry.register(ResourceHandlerGathering)
    i18n_dashboard.TranslatableResourceRegistry.register(
        TranslatableResourceGathering)


def register_module():
    """Registers this module in the registry."""

    handlers = [
        (handler.URL, handler) for handler in
        [GatheringsStudentHandler, GatheringsDashboardHandler]]

    dashboard.DashboardHandler.add_sub_nav_mapping(
        'analytics', MODULE_NAME, MODULE_TITLE,
        action=GatheringsDashboardHandler.LIST_ACTION,
        href=GatheringsDashboardHandler.LIST_URL,
        placement=1000, sub_group_name='pinned')

    dashboard.DashboardHandler.add_custom_post_action(
        GatheringsItemRESTHandler.STATUS_ACTION,
        GatheringsItemRESTHandler.post_set_draft_status)

    global custom_module  # pylint: disable=global-statement
    custom_module = custom_modules.Module(
        MODULE_TITLE,
        'A set of pages for managing course gatherings.',
        [], handlers, notify_module_enabled=on_module_enabled)
    return custom_module
