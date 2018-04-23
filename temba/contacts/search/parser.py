# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals

import operator

import regex
import six

from antlr4 import InputStream, CommonTokenStream, ParseTreeVisitor
from antlr4.error.Errors import ParseCancellationException, NoViableAltException
from antlr4.error.ErrorStrategy import BailErrorStrategy
from collections import OrderedDict
from decimal import Decimal
from django.db.models import Q, Func, Value as Val, CharField
from django.db.models.functions import Upper, Substr
from django.utils.encoding import force_text
from django.utils.translation import gettext as _
from elasticsearch_dsl import Q as es_Q
from functools import reduce
from temba.locations.models import AdminBoundary
from temba.utils.dates import str_to_datetime, date_to_utc_range
from temba.utils.es import ModelESSearch
from temba.values.models import Value
from temba.contacts.models import ContactField, ContactURN, Contact

# our index for equality checks on string values is limited to the first 32 characters
STRING_VALUE_COMPARISON_LIMIT = 32

BOUNDARY_LEVELS_BY_VALUE_TYPE = {
    Value.TYPE_STATE: AdminBoundary.LEVEL_STATE,
    Value.TYPE_DISTRICT: AdminBoundary.LEVEL_DISTRICT,
    Value.TYPE_WARD: AdminBoundary.LEVEL_WARD,
}

TEL_VALUE_REGEX = regex.compile(r'^[+ \d\-\(\)]+$', flags=regex.V0)
CLEAN_SPECIAL_CHARS_REGEX = regex.compile(r'[+ \-\(\)]+', flags=regex.V0)


class Concat(Func):
    """
    The Django Concat implementation splits arguments into pairs but we need to match the expression used on the index
    which is (contact_field_id || '|' || UPPER(string_value))
    """
    template = '(%(expressions)s)'
    arg_joiner = ' || '


@six.python_2_unicode_compatible
class SearchException(Exception):
    """
    Exception class for unparseable search queries
    """
    def __init__(self, message):
        self.message = message

    def __str__(self):
        return force_text(self.message)


@six.python_2_unicode_compatible
class ContactQuery(object):
    """
    A parsed contact query consisting of a hierarchy of conditions and boolean combinations of conditions
    """
    PROP_ATTRIBUTE = 'A'
    PROP_SCHEME = 'S'
    PROP_FIELD = 'F'

    SEARCHABLE_SCHEMES = ('tel', 'twitter')

    def __init__(self, root):
        self.root = root

    def optimized(self):
        return ContactQuery(self.root.simplify().split_by_prop())

    def as_query(self, org):
        prop_map = self.get_prop_map(org)

        return self.root.as_query(org, prop_map)

    def as_text(self):
        return self.root.as_text()

    def evaluate(self, org, contact_json):
        prop_map = self.get_prop_map(org)

        return self.root.evaluate(contact_json, prop_map)

    def as_elasticsearch(self, org):
        prop_map = self.get_prop_map(org)

        return self.root.as_elasticsearch(org, prop_map)

    def get_prop_map(self, org):
        """
        Recursively collects all property names from this query and tries to match them to fields, searchable attributes
        and URN schemes.
        """

        searchable_attrs = {'name'}
        if org.is_anon:
            searchable_attrs.update(['id'])

        all_props = set(self.root.get_prop_names())

        attr_props = all_props.difference(searchable_attrs)

        prop_map = {p: None for p in all_props}

        for field in ContactField.objects.filter(org=org, key__in=attr_props, is_active=True):
            prop_map[field.key] = (self.PROP_FIELD, field)

        for attr in searchable_attrs:
            if attr in prop_map.keys():
                prop_map[attr] = (self.PROP_ATTRIBUTE, attr)

        for scheme in self.SEARCHABLE_SCHEMES:
            if scheme in prop_map.keys():
                prop_map[scheme] = (self.PROP_SCHEME, scheme)

        for prop, prop_obj in prop_map.items():
            if not prop_obj:
                raise SearchException(_("Unrecognized field: %s") % prop)

        return prop_map

    def can_be_dynamic_group(self):
        props_not_allowed = {'name', 'id'}
        prop_names = set(self.root.get_prop_names())

        return not(prop_names.intersection(props_not_allowed))

    def __eq__(self, other):
        return isinstance(other, ContactQuery) and self.root == other.root

    def __str__(self):
        return six.text_type(self.root)

    def __repr__(self):
        return 'ContactQuery{%s}' % six.text_type(self)


class QueryNode(object):
    """
    A search query node which is either a condition or a boolean combination of other conditions
    """

    def simplify(self):
        return self

    def split_by_prop(self):
        return self

    def as_query(self, org, prop_map):  # pragma: no cover
        pass

    def as_text(self):  # pragma: no cover
        pass

    def as_elasticsearch(self, org, prop_map):  # pragma: no cover
        pass

    def evaluate(self, contact_json, prop_map):  # pragma: no cover
        pass


@six.python_2_unicode_compatible
class Condition(QueryNode):
    ATTR_OR_URN_LOOKUPS = {'=': 'iexact', '~': 'icontains'}

    TEXT_LOOKUPS = {'=': 'iexact'}

    NUMBER_LOOKUPS = {
        '=': 'exact',
        '>': 'gt',
        '>=': 'gte',
        '<': 'lt',
        '<=': 'lte'
    }

    DATETIME_LOOKUPS = {
        '=': '<equal>',
        '>': 'gt',
        '>=': 'gte',
        '<': 'lt',
        '<=': 'lte'
    }

    LOCATION_LOOKUPS = {'=': 'iexact'}

    COMPARATOR_ALIASES = {'is': '=', 'has': '~'}

    def __init__(self, prop, comparator, value):
        self.prop = prop
        self.comparator = self.COMPARATOR_ALIASES[comparator] if comparator in self.COMPARATOR_ALIASES else comparator
        self.value = value

    def get_prop_names(self):
        return [self.prop]

    def as_query(self, org, prop_map):
        prop_type, prop_obj = prop_map[self.prop]

        if prop_type == ContactQuery.PROP_FIELD:
            return self._build_value_query(prop_obj)
        elif prop_type == ContactQuery.PROP_SCHEME:
            if org.is_anon:
                return Q(id=-1)
            else:
                return self._build_urn_query(org, prop_obj)
        else:
            return self._build_attr_query(prop_obj)

    def _build_attr_query(self, attr):
        lookup = self.ATTR_OR_URN_LOOKUPS.get(self.comparator)
        if not lookup:
            raise SearchException(_("Can't query contact properties with %s") % self.comparator)

        return Q(**{'%s__%s' % (attr, lookup): self.value})

    def _build_urn_query(self, org, scheme):
        lookup = self.ATTR_OR_URN_LOOKUPS.get(self.comparator)
        if not lookup:
            raise SearchException(_("Can't query contact URNs with %s") % self.comparator)

        urns = ContactURN.objects.filter(**{'org': org, 'scheme': scheme, 'path__%s' % lookup: self.value})

        return Q(id__in=urns.values('contact_id'))

    def _build_value_query(self, field):
        value_contacts = self.get_base_value_query().filter(**self.build_value_query_params(field))

        return Q(id__in=value_contacts)

    def build_value_query_params(self, field):
        if field.value_type == Value.TYPE_TEXT:
            return self._build_text_field_params(field)
        elif field.value_type == Value.TYPE_NUMBER:
            return self._build_number_field_params(field)
        elif field.value_type == Value.TYPE_DATETIME:
            return self._build_datetime_field_params(field)
        elif field.value_type in (Value.TYPE_STATE, Value.TYPE_DISTRICT, Value.TYPE_WARD):
            return self._build_location_field_params(field)
        else:  # pragma: no cover
            raise ValueError("Unrecognized contact field type '%s'" % field.value_type)

    def _build_text_field_params(self, field):
        # combine field and value to match database index
        def index_key(f, val):
            return '%d|%s' % (f.id, val[:STRING_VALUE_COMPARISON_LIMIT].upper())

        if isinstance(self.value, list):
            return {'field_and_string_value__in': [index_key(field, v) for v in self.value]}
        else:
            if self.comparator not in self.TEXT_LOOKUPS:
                raise SearchException(_("Can't query text fields with %s") % self.comparator)

            return {'field_and_string_value': index_key(field, self.value)}

    def _build_number_field_params(self, field):
        if isinstance(self.value, list):
            return {'contact_field': field, 'decimal_value__in': [self._parse_number(v) for v in self.value]}
        else:
            lookup = self.NUMBER_LOOKUPS.get(self.comparator)
            if not lookup:
                raise SearchException(_("Can't query number fields with %s") % self.comparator)

            return {'contact_field': field, 'decimal_value__%s' % lookup: self._parse_number(self.value)}

    def _build_datetime_field_params(self, field):
        lookup = self.DATETIME_LOOKUPS.get(self.comparator)
        if not lookup:
            raise SearchException(_("Can't query date fields with %s") % self.comparator)

        # parse as localized date
        local_date = str_to_datetime(self.value, field.org.timezone, field.org.get_dayfirst(), fill_time=False)
        if not local_date:
            raise SearchException(_("Unable to parse the date %s") % self.value)

        # get the range of UTC datetimes for this local date
        utc_range = date_to_utc_range(local_date.date(), field.org)

        if lookup == '<equal>':
            return {'contact_field': field, 'datetime_value__gte': utc_range[0], 'datetime_value__lt': utc_range[1]}
        elif lookup == 'lt':
            return {'contact_field': field, 'datetime_value__lt': utc_range[0]}
        elif lookup == 'lte':
            return {'contact_field': field, 'datetime_value__lt': utc_range[1]}
        elif lookup == 'gt':
            return {'contact_field': field, 'datetime_value__gte': utc_range[1]}
        elif lookup == 'gte':
            return {'contact_field': field, 'datetime_value__gte': utc_range[0]}

    def _build_location_field_params(self, field):
        lookup = self.LOCATION_LOOKUPS.get(self.comparator)
        if not lookup:
            raise SearchException(_("Unsupported comparator %s for location field") % self.comparator)

        level_query = Q(level=BOUNDARY_LEVELS_BY_VALUE_TYPE.get(field.value_type))

        if isinstance(self.value, list):
            name_query = reduce(operator.or_, [Q(**{'name__%s' % lookup: v}) for v in self.value])
        else:
            name_query = Q(**{'name__%s' % lookup: self.value})

        locations = AdminBoundary.objects.filter(level_query & name_query)

        return {'contact_field': field, 'location_value__in': locations}

    @staticmethod
    def get_base_value_query():
        return Value.objects.annotate(
            field_and_string_value=Concat('contact_field_id', Val('|'), Upper(Substr('string_value', 1, STRING_VALUE_COMPARISON_LIMIT)), output_field=CharField())
        ).values('contact_id')

    @staticmethod
    def _parse_number(val):
        try:
            return Decimal(val)
        except Exception:
            raise SearchException(_("%s isn't a valid number") % val)

    def as_text(self):
        try:
            Decimal(self.value)
            is_decimal = True
        except Exception:
            is_decimal = False

        value = self.value if is_decimal else '"%s"' % self.value

        return '%s %s %s' % (self.prop, self.comparator, value)

    def evaluate(self, contact_json, prop_map):
        prop_type, field = prop_map[self.prop]

        if prop_type == ContactQuery.PROP_FIELD:
            field_uuid = six.text_type(field.uuid)
            contact_fields = contact_json.get('fields')

            if field_uuid not in contact_fields:
                return False

            if field.value_type == Value.TYPE_TEXT:
                query_value = self.value.upper()
                contact_value = contact_fields.get(field_uuid).get('text').upper()

                if self.comparator == '=':
                    return contact_value == query_value
                else:  # pragma: no cover
                    raise ValueError('Unknown text comparator: %s' % (self.comparator,))

            elif field.value_type == Value.TYPE_NUMBER:
                query_value = self._parse_number(self.value)

                number_value = contact_fields.get(field_uuid).get('number', contact_fields.get(field_uuid).get('decimal'))
                if number_value is None:
                    return False

                contact_value = self._parse_number(number_value)

                if self.comparator == '=':
                    return contact_value == query_value
                elif self.comparator == '>':
                    return contact_value > query_value
                elif self.comparator == '>=':
                    return contact_value >= query_value
                elif self.comparator == '<':
                    return contact_value < query_value
                elif self.comparator == '<=':
                    return contact_value <= query_value
                else:  # pragma: no cover
                    raise ValueError('Unknown number comparator: %s' % (self.comparator,))

            elif field.value_type == Value.TYPE_DATETIME:
                query_value = str_to_datetime(self.value, field.org.timezone, field.org.get_dayfirst(), fill_time=False)
                if not query_value:
                    raise SearchException(_("Unable to parse the date %s") % self.value)

                datetime_value = contact_fields.get(field_uuid).get('datetime')
                if datetime_value is None:
                    return False

                contact_value = str_to_datetime(datetime_value, field.org.timezone)

                utc_range = date_to_utc_range(query_value.date(), field.org)

                if self.comparator == '=':
                    return contact_value >= utc_range[0] and contact_value < utc_range[1]
                elif self.comparator == '>':
                    return contact_value >= utc_range[1]
                elif self.comparator == '>=':
                    return contact_value >= utc_range[0]
                elif self.comparator == '<':
                    return contact_value < utc_range[0]
                elif self.comparator == '<=':
                    return contact_value < utc_range[1]
                else:  # pragma: no cover
                    raise ValueError('Unknown datetime comparator: %s' % (self.comparator,))

            elif field.value_type in (Value.TYPE_STATE, Value.TYPE_DISTRICT, Value.TYPE_WARD):
                query_value = self.value.upper()

                if field.value_type == Value.TYPE_WARD:
                    ward_value = contact_fields.get(field_uuid).get('ward')
                    if ward_value is None:
                        ward_value = ""

                    contact_value = ward_value.upper().split(' > ')[-1]
                elif field.value_type == Value.TYPE_DISTRICT:
                    district_value = contact_fields.get(field_uuid).get('district')
                    if district_value is None:
                        district_value = ""

                    contact_value = district_value.upper().split(' > ')[-1]
                elif field.value_type == Value.TYPE_STATE:
                    state_value = contact_fields.get(field_uuid).get('state')
                    if state_value is None:
                        state_value = ""

                    contact_value = state_value.upper().split(' > ')[-1]
                else:  # pragma: no cover
                    raise ValueError('Unknown location type: %s' % (field.value_type, ))

                if self.comparator == '=':
                    return contact_value == query_value
                else:
                    raise SearchException(_("Unsupported comparator %s for location field") % self.comparator)

            else:  # pragma: no cover
                raise ValueError("Unrecognized contact field type '%s'" % field.value_type)

        elif prop_type == ContactQuery.PROP_SCHEME:
            for urn in contact_json.get('urns'):
                if urn.get('scheme') == field:
                    contact_value = urn.get('path').upper()
                    query_value = self.value.upper()

                    if self.comparator == '=':
                        if contact_value == query_value:
                            return True
                    elif self.comparator == '~':
                        if query_value in contact_value:
                            return True
                    else:  # pragma: no cover
                        raise ValueError('Unknown urn scheme comparator: %s' % (self.comparator,))

            return False

        else:
            raise ValueError("Unrecognized contact field type '%s'" % prop_type)

    def as_elasticsearch(self, org, prop_map):
        prop_type, field = prop_map[self.prop]

        if prop_type == ContactQuery.PROP_FIELD:
            field_uuid = six.text_type(field.uuid)
            es_query = es_Q('term', **{'fields.field': field_uuid})

            if field.value_type == Value.TYPE_TEXT:
                query_value = self.value.lower()

                if self.comparator == '=':
                    es_query &= es_Q('term', **{'fields.text': query_value})

                else:  # pragma: no cover
                    raise ValueError('Unknown text comparator: %s' % (self.comparator,))

            elif field.value_type == Value.TYPE_NUMBER:
                query_value = six.text_type(self._parse_number(self.value))

                if self.comparator == '=':
                    es_query &= es_Q('match', **{'fields.number': query_value})
                elif self.comparator == '>':
                    es_query &= es_Q('range', **{'fields.number': {'gt': query_value}})
                elif self.comparator == '>=':
                    es_query &= es_Q('range', **{'fields.number': {'gte': query_value}})
                elif self.comparator == '<':
                    es_query &= es_Q('range', **{'fields.number': {'lt': query_value}})
                elif self.comparator == '<=':
                    es_query &= es_Q('range', **{'fields.number': {'lte': query_value}})
                else:  # pragma: no cover
                    raise ValueError('Unknown number comparator: %s' % (self.comparator,))

            elif field.value_type == Value.TYPE_DATETIME:
                query_value = str_to_datetime(self.value, field.org.timezone, field.org.get_dayfirst(), fill_time=False)

                if not query_value:  # pragma: no cover
                    raise SearchException(_("Unable to parse the date %s") % self.value)

                utc_range = date_to_utc_range(query_value.date(), field.org)
                utc_date = six.text_type(utc_range[0].date())

                if self.comparator == '=':
                    es_query &= es_Q('match', **{'fields.datetime': utc_date})
                elif self.comparator == '>':
                    es_query &= es_Q('range', **{'fields.datetime': {'gt': utc_date}})
                elif self.comparator == '>=':
                    es_query &= es_Q('range', **{'fields.datetime': {'gte': utc_date}})
                elif self.comparator == '<':
                    es_query &= es_Q('range', **{'fields.datetime': {'lt': utc_date}})
                elif self.comparator == '<=':
                    es_query &= es_Q('range', **{'fields.datetime': {'lte': utc_date}})
                else:  # pragma: no cover
                    raise ValueError('Unknown datetime comparator: %s' % (self.comparator,))

            elif field.value_type in (Value.TYPE_STATE, Value.TYPE_DISTRICT, Value.TYPE_WARD):
                query_value = self.value.lower()

                if field.value_type == Value.TYPE_WARD:
                    field_name = 'fields.ward'
                elif field.value_type == Value.TYPE_DISTRICT:
                    field_name = 'fields.district'
                elif field.value_type == Value.TYPE_STATE:
                    field_name = 'fields.state'
                else:  # pragma: no cover
                    raise ValueError('Unknown location type: %s' % (field.value_type, ))

                if self.comparator == '=':
                    field_name += '.keyword'
                    es_query &= es_Q('term', **{field_name: query_value})
                else:
                    raise SearchException(_("Unsupported comparator %s for location field") % self.comparator)

            else:  # pragma: no cover
                raise ValueError("Unrecognized contact field type '%s'" % field.value_type)

            return es_Q(
                'nested', path='fields', query=es_query
            )

        elif prop_type == ContactQuery.PROP_ATTRIBUTE:
            query_value = self.value.lower()
            if field == 'name':
                if self.comparator == '=':
                    field_name = 'name.keyword'
                    es_query = es_Q('term', **{field_name: query_value})
                elif self.comparator == '~':
                    field_name = 'name'
                    es_query = es_Q('match', **{field_name: query_value})
                else:  # pragma: no cover
                    raise ValueError('Unknown attribute comparator: %s' % (self.comparator,))
            elif field == 'id':
                es_query = es_Q('ids', **{'values': [query_value]})
            else:  # pragma: no cover
                raise ValueError("Unknown attribute field '%s'" % (field, ))
            return es_query

        elif prop_type == ContactQuery.PROP_SCHEME:
            query_value = self.value.lower()
            es_query = es_Q('term', **{'urns.scheme': field.lower()})

            if org.is_anon:
                return es_Q('ids', **{'values': [-1]})
            else:
                if self.comparator == '=':
                    es_query &= es_Q('term', **{'urns.path.keyword': query_value})
                elif self.comparator == '~':
                    es_query &= es_Q('match_phrase', **{'urns.path': query_value})

                return es_Q('nested', path='urns', query=es_query)
        else:  # pragma: no cover
            raise ValueError("Unrecognized contact field type '%s'" % prop_type)

    def __eq__(self, other):
        return isinstance(other, Condition) and self.prop == other.prop and self.comparator == other.comparator and self.value == other.value

    def __str__(self):
        return '%s%s%s' % (self.prop, self.comparator, self.value)


class IsSetCondition(Condition):
    """
    A special type of condition which is just checking whether a property is set or not.
      * A condition of the form x != "" is interpreted as "x is set"
      * A condition of the form x = "" is interpreted as "x is not set"
    """
    IS_SET_LOOKUPS = ('!=',)
    IS_NOT_SET_LOOKUPS = ('is', '=')

    def __init__(self, prop, comparator):
        super(IsSetCondition, self).__init__(prop, comparator, "")

    def as_query(self, org, prop_map):
        prop_type, prop_obj = prop_map[self.prop]

        if self.comparator.lower() in self.IS_SET_LOOKUPS:
            is_set = True
        elif self.comparator.lower() in self.IS_NOT_SET_LOOKUPS:
            is_set = False
        else:
            raise SearchException(_("Invalid operator for empty string comparison"))

        if prop_type == ContactQuery.PROP_FIELD:
            values_query = Value.objects.filter(contact_field=prop_obj).values('contact_id')

            if prop_obj.value_type == Value.TYPE_TEXT:
                values_query = values_query.filter(string_value__isnull=False)
            elif prop_obj.value_type == Value.TYPE_NUMBER:
                values_query = values_query.filter(decimal_value__isnull=False)
            elif prop_obj.value_type == Value.TYPE_DATETIME:
                values_query = values_query.filter(datetime_value__isnull=False)
            elif prop_obj.value_type in (Value.TYPE_STATE, Value.TYPE_DISTRICT, Value.TYPE_WARD):
                values_query = values_query.filter(location_value__isnull=False)
            else:  # pragma: no cover
                raise ValueError("Unrecognized contact field type '%s'" % prop_obj.value_type)

            return Q(id__in=values_query) if is_set else ~Q(id__in=values_query)

        elif prop_type == ContactQuery.PROP_SCHEME:
            if org.is_anon:
                return Q(id=-1)
            else:
                urns_query = ContactURN.objects.filter(org=org, scheme=prop_obj).values('contact_id')

                return Q(id__in=urns_query) if is_set else ~Q(id__in=urns_query)
        else:
            # for attributes, being not-set can mean a NULL value or empty string value
            where_not_set = Q(**{prop_obj: ""}) | Q(**{prop_obj: None})
            return ~where_not_set if is_set else where_not_set

    def evaluate(self, contact_json, prop_map):
        prop_type, field = prop_map[self.prop]

        if self.comparator.lower() in self.IS_SET_LOOKUPS:
            is_set = True
        elif self.comparator.lower() in self.IS_NOT_SET_LOOKUPS:
            is_set = False
        else:  # pragma: no cover
            raise SearchException(_("Invalid operator for empty string comparison"))

        if prop_type == ContactQuery.PROP_FIELD:
            field_uuid = six.text_type(field.uuid)
            contact_fields = contact_json.get('fields')

            contact_field = contact_fields.get(field_uuid)

            # contact field does not exist
            if contact_field is None:
                if is_set:
                    return False
                else:
                    return True
            else:
                if field.value_type == Value.TYPE_TEXT:
                    contact_value = contact_field.get('text')
                    if is_set:
                        if contact_value is not None:
                            return True
                        else:  # pragma: can't cover
                            return False
                    else:
                        if contact_value is not None:
                            return False
                        else:  # pragma: can't cover
                            return True
                elif field.value_type == Value.TYPE_NUMBER:
                    try:
                        contact_value = self._parse_number(contact_field.get('decimal', contact_field.get('number')))
                    except SearchException:
                        contact_value = None

                    if is_set:
                        if contact_value is not None:
                            return True
                        else:
                            return False
                    else:
                        if contact_value is not None:
                            return False
                        else:
                            return True

                elif field.value_type == Value.TYPE_DATETIME:
                    contact_value = str_to_datetime(contact_field.get('datetime'), field.org.timezone)
                    if is_set:
                        if contact_value is not None:
                            return True
                        else:
                            return False
                    else:
                        if contact_value is not None:
                            return False
                        else:
                            return True

                elif field.value_type == Value.TYPE_WARD:
                    contact_value = contact_field.get('ward')
                    if is_set:
                        if contact_value is not None:
                            return True
                        else:
                            return False
                    else:
                        if contact_value is not None:
                            return False
                        else:
                            return True

                elif field.value_type == Value.TYPE_DISTRICT:
                    contact_value = contact_field.get('district')
                    if is_set:
                        if contact_value is not None:
                            return True
                        else:
                            return False
                    else:
                        if contact_value is not None:
                            return False
                        else:
                            return True

                elif field.value_type == Value.TYPE_STATE:
                    contact_value = contact_field.get('state')
                    if is_set:
                        if contact_value is not None:
                            return True
                        else:
                            return False
                    else:
                        if contact_value is not None:
                            return False
                        else:
                            return True

                else:  # pragma: no cover
                    raise ValueError("Unrecognized contact field type '%s'" % field.value_type)

        elif prop_type == ContactQuery.PROP_SCHEME:
            urn_exists = next((urn for urn in contact_json.get('urns') if urn.get('scheme') == field), None)

            if not urn_exists:
                if is_set:
                    return False
                else:
                    return True
            else:
                if is_set:
                    return True
                else:
                    return False

        else:
            raise ValueError("Unrecognized contact field type '%s'" % prop_type)

    def as_elasticsearch(self, org, prop_map):
        prop_type, field = prop_map[self.prop]

        if self.comparator.lower() in self.IS_SET_LOOKUPS:
            is_set = True
        elif self.comparator.lower() in self.IS_NOT_SET_LOOKUPS:
            is_set = False
        else:  # pragma: no cover
            raise SearchException(_("Invalid operator for empty string comparison"))

        if prop_type == ContactQuery.PROP_FIELD:
            field_uuid = six.text_type(field.uuid)
            es_query = es_Q('term', **{'fields.field': field_uuid})

            if field.value_type == Value.TYPE_TEXT:
                field_name = 'fields.text'
            elif field.value_type == Value.TYPE_NUMBER:
                field_name = 'fields.number'
            elif field.value_type == Value.TYPE_DATETIME:
                field_name = 'fields.datetime'
            elif field.value_type == Value.TYPE_STATE:
                field_name = 'fields.state'
            elif field.value_type == Value.TYPE_DISTRICT:
                field_name = 'fields.district'
            elif field.value_type == Value.TYPE_WARD:
                field_name = 'fields.ward'
            else:  # pragma: no cover
                raise ValueError("Unrecognized contact field type '%s'" % (field.value_type, ))

            es_query &= es_Q('exists', **{'field': field_name})

            if is_set:
                return es_Q('nested', path='fields', query=es_query)
            else:
                return ~es_Q('nested', path='fields', query=es_query)
        elif prop_type == ContactQuery.PROP_SCHEME:
            if org.is_anon:
                return es_Q('ids', **{'values': [-1]})

            es_query = es_Q('exists', **{'field': 'urns.path'}) & es_Q('term', **{'urns.scheme': field.lower()})

            if is_set:
                return es_Q('nested', path='urns', query=es_query)
            else:
                return ~es_Q('nested', path='urns', query=es_query)
        elif prop_type == ContactQuery.PROP_ATTRIBUTE:
            if field == 'name':
                if is_set:
                    es_query = ~es_Q('term', **{'name': ''})
                else:
                    es_query = es_Q('term', **{'name': ''})
                return es_query
            elif field == 'id':
                raise SearchException("All contacts have an ID, you cannot check if 'id' is set")
            else:  # pragma: no cover
                raise ValueError("Unknown attribute field '%s'" % (field, ))
        else:  # pragma: no cover
            raise ValueError("Unrecognized contact field type '%s'" % (prop_type, ))


@six.python_2_unicode_compatible
class BoolCombination(QueryNode):
    """
    A combination of two or more conditions using an AND or OR logical operation
    """
    AND = operator.and_
    OR = operator.or_

    def __init__(self, op, *children):
        self.op = op
        self.children = list(children)

    def get_prop_names(self):
        names = []
        for child in self.children:
            names += child.get_prop_names()
        return names

    def simplify(self):
        """
        The expression `x OR y OR z` will be parsed as `OR(OR(x, y), z)` but because the logical operators AND/OR are
        associative we can simplify this as `OR(x, y, z)`.
        """
        self.children = [c.simplify() for c in self.children]  # simplify our children first

        simplified = []

        for child in self.children:
            if isinstance(child, Condition):
                simplified.append(child)
            elif child.op != self.op:
                return self  # can't optimize if children are combined with a different boolean op
            else:
                simplified += child.children

        return BoolCombination(self.op, *simplified)

    def split_by_prop(self):
        """
        The expression `OR(a=1, b=2, a=3)` can be re-arranged to `OR(OR(a=1, a=3), b=2)` so that `a=1 OR a=3` can be
        more efficiently checked using a single query on `a`.
        """
        self.children = [c.split_by_prop() for c in self.children]  # split our children first

        children_by_prop = OrderedDict()
        for child in self.children:
            prop = child.prop if isinstance(child, Condition) else None
            if prop not in children_by_prop:
                children_by_prop[prop] = []
            children_by_prop[prop].append(child)

        new_children = []
        for prop, children in children_by_prop.items():
            if len(children) > 1 and prop is not None:
                new_children.append(SinglePropCombination(prop, self.op, *children))
            else:
                new_children += children

        if len(new_children) == 1:
            return new_children[0]

        return BoolCombination(self.op, *new_children)

    def as_query(self, org, prop_map):
        return reduce(self.op, [child.as_query(org, prop_map) for child in self.children])

    def evaluate(self, contact_json, prop_map):
        return reduce(self.op, [child.evaluate(contact_json, prop_map) for child in self.children])

    def as_elasticsearch(self, org, prop_map):
        return reduce(self.op, [child.as_elasticsearch(org, prop_map) for child in self.children])

    def as_text(self):
        op = ' OR ' if self.op == self.OR else ' AND '
        children = []
        for c in self.children:
            if isinstance(c, BoolCombination):
                children.append('(%s)' % c.as_text())
            else:
                children.append(c.as_text())

        return op.join(children)

    def __eq__(self, other):
        return isinstance(other, BoolCombination) and self.op == other.op and self.children == other.children

    def __str__(self):
        op = 'OR' if self.op == self.OR else 'AND'
        return '%s(%s)' % (op, ', '.join([six.text_type(c) for c in self.children]))


@six.python_2_unicode_compatible
class SinglePropCombination(BoolCombination):
    """
    A special case combination where all conditions are on the same property and so may be optimized to query the value
    table only once.
    """
    def __init__(self, prop, op, *children):
        assert all([isinstance(c, Condition) and c.prop == prop for c in children])

        self.prop = prop

        super(SinglePropCombination, self).__init__(op, *children)

    def as_query(self, org, prop_map):
        prop_type, prop_obj = prop_map[self.prop]

        if prop_type == ContactQuery.PROP_FIELD:

            # a sequence of OR'd equality checks can be further optimized (e.g. `a = 1 OR a = 2` as `a IN (1, 2)`)
            # except for datetime fields as equality is implemented as a range check
            all_equality = all([child.comparator == '=' for child in self.children])
            if self.op == BoolCombination.OR and all_equality and prop_obj.value_type != Value.TYPE_DATETIME:
                in_condition = Condition(self.prop, '=', [c.value for c in self.children])
                return in_condition.as_query(org, prop_map)

            # otherwise just combine the Value sub-queries into a single one
            value_queries = []
            for child in self.children:
                params = child.build_value_query_params(prop_obj)
                value_queries.append(Q(**params))

            value_query = reduce(self.op, value_queries)
            value_contacts = Condition.get_base_value_query().filter(value_query).values('contact_id')

            return Q(id__in=value_contacts)

        return super(SinglePropCombination, self).as_query(org, prop_map)

    def __eq__(self, other):
        return isinstance(other, SinglePropCombination) and self.prop == other.prop and super(SinglePropCombination, self).__eq__(other)

    def __str__(self):
        op = 'OR' if self.op == self.OR else 'AND'
        return '%s[%s](%s)' % (op, self.prop, ', '.join(['%s%s' % (c.comparator, c.value) for c in self.children]))


class ContactQLVisitor(ParseTreeVisitor):

    def __init__(self, as_anon):
        self.as_anon = as_anon

    def visitParse(self, ctx):
        return self.visit(ctx.expression())

    def visitImplicitCondition(self, ctx):
        """
        expression : TEXT
        """
        value = ctx.TEXT().getText()

        if self.as_anon:
            try:
                value = int(value)
                return Condition('id', '=', str(value))
            except ValueError:
                pass
        elif TEL_VALUE_REGEX.match(value):
            return Condition('tel', '~', value)

        return Condition('name', '~', value)

    def visitCondition(self, ctx):
        """
        expression : TEXT COMPARATOR literal
        """
        prop = ctx.TEXT().getText().lower()
        comparator = ctx.COMPARATOR().getText().lower()
        value = self.visit(ctx.literal())

        if value == "":
            return IsSetCondition(prop, comparator)
        else:
            return Condition(prop, comparator, value)

    def visitCombinationAnd(self, ctx):
        """
        expression : expression AND expression
        """
        return BoolCombination(BoolCombination.AND, self.visit(ctx.expression(0)), self.visit(ctx.expression(1)))

    def visitCombinationImpicitAnd(self, ctx):
        """
        expression : expression expression
        """
        return BoolCombination(BoolCombination.AND, self.visit(ctx.expression(0)), self.visit(ctx.expression(1)))

    def visitCombinationOr(self, ctx):
        """
        expression : expression OR expression
        """
        return BoolCombination(BoolCombination.OR, self.visit(ctx.expression(0)), self.visit(ctx.expression(1)))

    def visitExpressionGrouping(self, ctx):
        """
        expression : LPAREN expression RPAREN
        """
        return self.visit(ctx.expression())

    def visitTextLiteral(self, ctx):
        """
        literal : TEXT
        """
        return ctx.getText()

    def visitStringLiteral(self, ctx):
        """
        literal : STRING
        """
        value = ctx.getText()[1:-1]
        return value.replace('""', '"')  # unescape embedded quotes


def parse_query(text, optimize=True, as_anon=False):
    """
    Parses the given contact query and optionally optimizes it
    """
    from .gen.ContactQLLexer import ContactQLLexer
    from .gen.ContactQLParser import ContactQLParser

    is_phone, cleaned_phone = is_phonenumber(text)

    if not as_anon and is_phone:
        stream = InputStream(cleaned_phone)
    else:
        stream = InputStream(text)

    lexer = ContactQLLexer(stream)
    tokens = CommonTokenStream(lexer)
    parser = ContactQLParser(tokens)
    parser._errHandler = BailErrorStrategy()

    try:
        tree = parser.parse()
    except ParseCancellationException as ex:
        message = None
        if ex.args and isinstance(ex.args[0], NoViableAltException):
            token = ex.args[0].offendingToken
            if token is not None and token.type != ContactQLParser.EOF:
                message = "Search query contains an error at: %s" % token.text

        if message is None:
            message = "Search query contains an error"

        raise SearchException(message)

    visitor = ContactQLVisitor(as_anon)

    query = ContactQuery(visitor.visit(tree))
    return query.optimized() if optimize else query


def evaluate_query(org, text, contact_json=dict):
    parsed = parse_query(text, optimize=True, as_anon=org.is_anon)

    return parsed.evaluate(org, contact_json)


def contact_search(org, text, base_queryset):
    """
    Performs the given contact query on the given base queryset
    """
    parsed = parse_query(text, as_anon=org.is_anon)
    query = parsed.as_query(org)

    return base_queryset.filter(org=org).filter(query), parsed


def contact_es_search(org, text, base_group=None):
    """
    Returns ES query
    """

    if not base_group:
        base_group = org.cached_all_contacts_group

    es_filter = es_Q('bool', filter=[
        # es_Q('term', is_blocked=False),
        # es_Q('term', is_stopped=False),
        es_Q('term', org_id=org.id),
        es_Q('term', groups=six.text_type(base_group.uuid))
    ])

    parsed = parse_query(text, as_anon=org.is_anon)
    es_match = parsed.as_elasticsearch(org)

    return (
        ModelESSearch(model=Contact, index='contacts')
        .params(routing=org.id)
        .query(es_match & es_filter)
        .sort('-modified_on_mu')
    )


def extract_fields(org, text):
    """
    Extracts contact fields from the given text query
    """
    parsed = parse_query(text, as_anon=org.is_anon)
    prop_map = parsed.get_prop_map(org)
    return [prop_obj for (prop_type, prop_obj) in prop_map.values() if prop_type == ContactQuery.PROP_FIELD]


def is_phonenumber(text):
    """
    Checks if query looks like a phone number, and if so returns a cleaned version of it
    """
    matches = TEL_VALUE_REGEX.match(text)
    if matches:
        return True, CLEAN_SPECIAL_CHARS_REGEX.sub('', text)
    else:
        return False, None
