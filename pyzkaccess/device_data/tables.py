from datetime import datetime, time, date
from enum import Enum
from typing import Mapping, MutableMapping, Callable, Optional, Type, TypeVar, Any

from ..common import ZKDatetimeUtils
from ..enums import (
    HolidayLoop,
    VerifyMode,
    PassageDirection,
    EVENT_TYPES,
    INOUTFUN_INPUT,
    INOUTFUN_OUTPUT,
    InOutFunRelayGroup
)

data_table_classes = {}  # type: MutableMapping[str, Type[DataTable]]


FieldDataT = TypeVar('FieldDataT')


class Field:
    """This class is used to define a field in DataTable. The property
    it assignes to will be used to access to an appropriate table field.
    In other words it provides object access to that field.

    Every field in device tables stores as a string, but some of
    them have a certain data format which could be represented
    with python types. Also some of them may have value restrictions.
    All of these parameters may be specified in Field definition as
    data type, convertion and validation callbacks. By default a
    field is treated as string with no restrictions.
    """
    def __init__(self,
                 raw_name: str,
                 field_datatype: Type = str,
                 get_cb: Optional[Callable[[str], Any]] = None,
                 set_cb: Optional[Callable[[Any], Any]] = None,
                 validation_cb: Optional[Callable[[FieldDataT], bool]] = None):
        """
        On getting a field value from DataTable record, the process is:
         1. Retrieve raw field value of `raw_name`. If nothing then
            just return None
         2. If `get_cb` is set then call it and use its result as value
         3. If value is not instance of `field_datatype` then try to
            cast it to this type
         4. Return value as field value

        On setting a field value in DataTable record, the process is:
         1. Check if value has `field_datatype` type, raise an error
            if not
         2. If `validation_cb` is set then call it, if result is false
            then raise an error
         3. Extract Enum value if value is Enum
         4. If `set_cb` is set then call it and use its result as value
         5. Write `str(value)` to raw field value of `raw_name`

        :param raw_name: field name in device table which this field
         associated to
        :param field_datatype: type of data of this field. `str` by
         default
        :param get_cb: optional callback that is called on field get
         before a raw string value will be converted to `field_datatype`
        :param set_cb: optional callback that is called on field set
         after value will be checked against `field_datatype`
         and validated by `validation_cb`
        :param validation_cb: optional callback that is called on
         field set after value will be checked against `field_datatype`.
         If returns false then validation will be failed
        """
        self._raw_name = raw_name
        self._field_datatype = field_datatype
        self._get_cb = get_cb
        self._set_cb = set_cb
        self._validation_cb = validation_cb

    @property
    def raw_name(self) -> str:
        """Raw field name in device table which this field
        associated to"""
        return self._raw_name

    def to_raw_value(self, value: Any) -> str:
        """Convert value of `field_datatype` to a raw string value.
        This function typically calls on field set.

        Checks incoming value against `field_datatype`, validates it
        using `validation_cb` (if any) and converts it using `set_cb`
        (if any).
        :param value: value of `field_datatype`
        :return: raw value string representation
        """
        if not isinstance(value, self._field_datatype):
            raise TypeError(
                'Bad value type {}, must be {}'.format(type(value), self._field_datatype)
            )

        if not(self._validation_cb is None or self._validation_cb(value)):
            raise ValueError('Value {} does not meet to field restrictions'.format(value))

        if isinstance(value, Enum):
            value = value.value

        if self._set_cb is not None:
            value = self._set_cb(value)

        return str(value)

    def to_field_value(self, value: str) -> FieldDataT:
        """Convert raw string value to a value of `field_datatype`.
        This function typically calls on field get.

        Converts incoming value using `get_cb` (if any). If
        type of value after that is not an instance of `field_datatype`,
        then tries to cast value to `field_datatype` (if specified).
        :param value: raw string representation
        :return: value of `field_datatype`
        """
        if self._get_cb is not None:
            value = self._get_cb(value)
        if not isinstance(value, self._field_datatype):
            value = self._field_datatype(value)

        return value

    def __hash__(self):
        return hash(self._raw_name)

    def __get__(self, instance, owner):
        if instance is None:
            return self

        value = instance._raw_data.get(self._raw_name)  # type: Optional[str]
        if value is not None:
            value = self.to_field_value(value)

        return value

    def __set__(self, instance, value):
        if instance is None:
            return

        if value is None:
            self.__delete__(instance)
            return

        raw_value = self.to_raw_value(value)
        instance._raw_data[self._raw_name] = raw_value  # noqa
        instance._dirty = True

    def __delete__(self, instance):
        if instance is None:
            return

        if self._raw_name in instance._raw_data:
            del instance._raw_data[self._raw_name]
            instance._dirty = True


class DataTableMetadata(type):
    def __new__(mcs, name, bases, attrs):
        attrs['_fields_mapping'] = {}
        for attr_name, attr in attrs.items():
            if isinstance(attr, Field):
                attrs['_fields_mapping'][attr_name] = attr.raw_name

        klass = super(DataTableMetadata, mcs).__new__(mcs, name, bases, attrs)
        data_table_classes[name] = klass  # noqa
        return klass


class DataTable(metaclass=DataTableMetadata):
    """Base class for models that represent device data tables.

    A concrete model contains device table name and field definitions.
    Also it provides interface to access to these fields in a concrete
    row and to manipulate that row.
    """
    table_name = None

    _fields_mapping = None

    def __init__(self, **fields):
        """Accepts initial fields data in kwargs"""
        self._sdk = None
        self._dirty = True
        self._raw_data = {}  # type: Mapping[str, str]

        fm = self._fields_mapping
        if fields:
            extra_keys = fields.keys() - fm.keys()
            if extra_keys:
                raise TypeError('Unknown fields: {}'.format(tuple(extra_keys)))

            self._raw_data = {
                fm[field]: getattr(self.__class__, field).to_raw_value(fields.get(field))
                for field in fm.keys() & fields.keys()
            }

    def delete(self):
        """Delete this record from a table"""
        if self._sdk is None:
            raise TypeError('Unable to delete a manually created data table record')

        gen = self._sdk.delete_device_data(self.table_name)
        gen.send(None)
        gen.send(self.raw_data)
        try:
            gen.send(None)
        except StopIteration:
            pass

        self._dirty = True

    def save(self):
        """Save changes in this record"""
        if self._sdk is None:
            raise TypeError('Unable to save a manually created data table record')

        gen = self._sdk.set_device_data(self.table_name)
        gen.send(None)
        gen.send(self.raw_data)
        try:
            gen.send(None)
        except StopIteration:
            pass

        self._dirty = False

    @property
    def data(self) -> Mapping[str, FieldDataT]:
        """Return record data as a dict"""
        return {field: self._raw_data.get(key)
                for field, key in self._fields_mapping.items()}

    @property
    def raw_data(self) -> Mapping[str, str]:
        """Return raw data written directly to the device table on
        save"""
        return self._raw_data

    @classmethod
    def fields_mapping(cls) -> Mapping[str, str]:
        """Mapping between model fields and their raw fields"""
        return cls._fields_mapping

    def with_raw_data(self, raw_data: Mapping[str, str], dirty: bool = True) -> 'DataTable':
        self._raw_data = raw_data
        self._dirty = dirty
        return self

    def with_sdk(self, sdk) -> 'DataTable':
        self._sdk = sdk
        return self

    def __repr__(self):
        return '{}{}({})'.format('*' if self._dirty else '',
                                 self.__class__.__name__,
                                 ', '.join('{}={}'.format(k, v) for k, v in self.data.items()))


class User(DataTable):
    """Card number information table"""
    table_name = 'user'

    card = Field('CardNo')
    pin = Field('Pin')
    password = Field('Password')
    group = Field('Group')
    start_time = Field(
        'StartTime', date, ZKDatetimeUtils.zkdate_to_date, ZKDatetimeUtils.date_to_zkdate
    )
    end_time = Field(
        'EndTime', date, ZKDatetimeUtils.zkdate_to_date, ZKDatetimeUtils.date_to_zkdate
    )
    super_authorize = Field('SuperAuthorize', bool, int, int)


class UserAuthorize(DataTable):
    """Access privilege list"""
    table_name = 'userauthorize'

    pin = Field('Pin')
    timezone_id = Field('AuthorizeTimezoneId', int)
    # tuple with 4 booleans (lock1..lock4)
    doors = Field(
        'AuthorizeDoorId',
        tuple,
        lambda x: (bool(i) for i in '{:04b}'.format(int(x))[::-1]),
        lambda x: int(''.join(x[::-1]), 2),
        lambda x: len(x) == 4
    )


class Holiday(DataTable):
    """Holiday table"""
    table_name = 'holiday'

    holiday = Field('Holiday')
    holiday_type = Field('HolidayType', int, None, None, lambda x: 1 <= x <= 3)
    loop = Field('Loop', HolidayLoop, int)


def _tz_encode(value: tuple):
    return ZKDatetimeUtils.times_to_zktimerange(value[0], value[1])


_tz_decode = ZKDatetimeUtils.zktimerange_to_times


def _tz_validate(value: tuple) -> bool:
    return len(value) == 2 and all(isinstance(x, (time, datetime)) for x in value)


class Timezone(DataTable):
    """Time zone table"""
    table_name = 'timezone'

    timezone_id = Field('TimezoneId')
    # Segment 1
    sun_time1 = Field('SunTime1', tuple, _tz_decode, _tz_encode, _tz_validate)
    mon_time1 = Field('MonTime1', tuple, _tz_decode, _tz_encode, _tz_validate)
    tue_time1 = Field('TueTime1', tuple, _tz_decode, _tz_encode, _tz_validate)
    wed_time1 = Field('WedTime1', tuple, _tz_decode, _tz_encode, _tz_validate)
    thu_time1 = Field('ThuTime1', tuple, _tz_decode, _tz_encode, _tz_validate)
    fri_time1 = Field('FriTime1', tuple, _tz_decode, _tz_encode, _tz_validate)
    sat_time1 = Field('SatTime1', tuple, _tz_decode, _tz_encode, _tz_validate)
    hol1_time1 = Field('Hol1Time1', tuple, _tz_decode, _tz_encode, _tz_validate)
    hol2_time1 = Field('Hol2Time1', tuple, _tz_decode, _tz_encode, _tz_validate)
    hol3_time1 = Field('Hol3Time1', tuple, _tz_decode, _tz_encode, _tz_validate)
    # Segment 2
    sun_time2 = Field('SunTime2', tuple, _tz_decode, _tz_encode, _tz_validate)
    mon_time2 = Field('MonTime2', tuple, _tz_decode, _tz_encode, _tz_validate)
    tue_time2 = Field('TueTime2', tuple, _tz_decode, _tz_encode, _tz_validate)
    wed_time2 = Field('WedTime2', tuple, _tz_decode, _tz_encode, _tz_validate)
    thu_time2 = Field('ThuTime2', tuple, _tz_decode, _tz_encode, _tz_validate)
    fri_time2 = Field('FriTime2', tuple, _tz_decode, _tz_encode, _tz_validate)
    sat_time2 = Field('SatTime2', tuple, _tz_decode, _tz_encode, _tz_validate)
    hol1_time2 = Field('Hol1Time2', tuple, _tz_decode, _tz_encode, _tz_validate)
    hol2_time2 = Field('Hol2Time2', tuple, _tz_decode, _tz_encode, _tz_validate)
    hol3_time2 = Field('Hol3Time2', tuple, _tz_decode, _tz_encode, _tz_validate)
    # Segment 3
    sun_time3 = Field('SunTime2', tuple, _tz_decode, _tz_encode, _tz_validate)
    mon_time3 = Field('MonTime2', tuple, _tz_decode, _tz_encode, _tz_validate)
    tue_time3 = Field('TueTime2', tuple, _tz_decode, _tz_encode, _tz_validate)
    wed_time3 = Field('WedTime2', tuple, _tz_decode, _tz_encode, _tz_validate)
    thu_time3 = Field('ThuTime2', tuple, _tz_decode, _tz_encode, _tz_validate)
    fri_time3 = Field('FriTime2', tuple, _tz_decode, _tz_encode, _tz_validate)
    sat_time3 = Field('SatTime2', tuple, _tz_decode, _tz_encode, _tz_validate)
    hol1_time3 = Field('Hol1Time3', tuple, _tz_decode, _tz_encode, _tz_validate)
    hol2_time3 = Field('Hol2Time3', tuple, _tz_decode, _tz_encode, _tz_validate)
    hol3_time3 = Field('Hol3Time3', tuple, _tz_decode, _tz_encode, _tz_validate)


class Transaction(DataTable):
    """Access control record table"""
    table_name = 'transaction'

    card = Field('Cardno')
    pin = Field('Pin')
    verify_mode = Field('Verified', VerifyMode, int, int)
    door = Field('DoorID', int)
    event_type = Field(
        'EventType', int, lambda x: EVENT_TYPES[int(x)], None, lambda x: x in EVENT_TYPES
    )
    entry_exit = Field('InOutState', PassageDirection, int, int)
    time = Field(
        'Time_second',
        datetime,
        ZKDatetimeUtils.zkctime_to_datetime,
        ZKDatetimeUtils.datetime_to_zkctime
    )


class FirstCard(DataTable):
    """First-card door opening"""
    table_name = 'firstcard'

    door = Field('DoorID', int)
    pin = Field('Pin')
    timezone_id = Field('TimezoneID', int)


class MultiCard(DataTable):
    """Multi-card door opening"""
    table_name = 'multimcard'   # Yes, typo in table name

    index = Field('Index')
    door = Field('DoorId', int)
    group1 = Field('Group1')
    group2 = Field('Group2')
    group3 = Field('Group3')
    group4 = Field('Group4')
    group5 = Field('Group5')


class InOutFun(DataTable):
    """Linkage control I/O table"""
    table_name = 'inoutfun'

    index = Field('Index')
    event_type = Field(
        'EventType', int, lambda x: EVENT_TYPES[int(x)], None, lambda x: x in EVENT_TYPES
    )
    input_index = Field(
        'InAddr', int, lambda x: INOUTFUN_INPUT[int(x)], None, lambda x: x in INOUTFUN_INPUT
    )
    is_output = Field('OutType', InOutFunRelayGroup)
    output_index = Field(
        'OutAddr',
        int,
        lambda x: INOUTFUN_OUTPUT[int(x)],
        None,
        lambda x: x in INOUTFUN_OUTPUT
    )
    time = Field('OutTime')  # FIXME: specify data type; can't test now
    reserved = Field('Reserved')


class TemplateV10(DataTable):
    """templatev10 table. No information"""
    table_name = 'templatev10'

    size = Field('Size')
    uid = Field('UID')
    pin = Field('Pin')
    finger_id = Field('FingerID')
    valid = Field('Valid')
    template = Field('Template')
    resverd = Field('Resverd')
    end_tag = Field('EndTag')
