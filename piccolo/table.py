from __future__ import annotations
import copy
from dataclasses import dataclass, field
import typing as t

from piccolo.engine import Engine, engine_finder
from piccolo.columns import Column, PrimaryKey, ForeignKey
from piccolo.query import (
    Alter,
    Create,
    Count,
    Delete,
    Drop,
    Exists,
    Insert,
    Objects,
    Raw,
    Select,
    TableExists,
    Update,
)
from piccolo.querystring import QueryString, Unquoted
from piccolo.utils import _camel_to_snake


@dataclass
class TableMeta:
    """
    This is used to store info about the table.
    """

    tablename: str = ""
    columns: t.List[Column] = field(default_factory=list)
    non_default_columns: t.List[Column] = field(default_factory=list)
    db: t.Optional[Engine] = engine_finder()


class TableMetaclass(type):
    def __str__(cls):
        """
        Returns a basic string representation of the table and its columns.

        Used by the playground.
        """
        spacer = "\n    "
        columns = []
        for col in cls._meta.columns:
            if type(col) == ForeignKey:
                columns.append(
                    f"{col._meta.name} = ForeignKey({col._foreign_key_meta.references.__name__})"
                )
            else:
                columns.append(f"{col._meta.name} = {col.__class__.__name__}()")
        columns_string = spacer.join(columns)
        return f"class {cls.__name__}(Table):\n" f"    {columns_string}\n"


class Table(metaclass=TableMetaclass):

    # These are just placeholder values, so type inference isn't confused - the
    # actual values are set in __init_subclass__.
    _meta = TableMeta()
    id = PrimaryKey()

    def __init_subclass__(
        cls, tablename: t.Optional[str] = None, db: t.Optional[Engine] = None
    ):
        """
        Automatically populate the _meta, which includes the tablename, and
        columns.
        """
        cls.id = PrimaryKey()

        tablename = tablename if tablename else _camel_to_snake(cls.__name__)

        attribute_names = [i for i in dir(cls) if not i.startswith("_")]

        columns: t.List[Column] = []
        non_default_columns: t.List[Column] = []

        for attribute_name in attribute_names:
            attribute = getattr(cls, attribute_name)
            if isinstance(attribute, Column):
                column = attribute
                columns.append(column)

                if not isinstance(column, PrimaryKey):
                    non_default_columns.append(column)

                column._meta._name = attribute_name
                column._meta._table = cls

        db = db if db else engine_finder()

        cls._meta = TableMeta(
            tablename=tablename, columns=columns, non_default_columns=[], db=db
        )

    def __init__(self, **kwargs):
        """
        Assigns any default column values to the class.
        """
        for column in self._meta.columns:
            value = kwargs.pop(column._meta.name, None)
            if not value:
                if column.default:
                    # Can't use inspect - can't tell that datetime.datetime.now
                    # is a callable.
                    is_callable = hasattr(column.default, "__call__")
                    value = column.default() if is_callable else column.default
                else:
                    if not column._meta.null:
                        raise ValueError(f"{column._meta.name} wasn't provided")
            self[column._meta.name] = value

        unrecognized = kwargs.keys()
        if unrecognized:
            raise ValueError(f"Unrecognized columns - {unrecognized}")

    def save(self) -> t.Union[Insert, Update]:
        """
        A proxy to an insert or update query.
        """
        if not hasattr(self, "id"):
            raise ValueError("No id value found")

        cls = self.__class__

        if type(self.id) == int:
            # pre-existing row
            kwargs = {
                i: getattr(self, i._meta.name, None) for i in cls._meta.columns
            }
            _id = kwargs.pop("id")
            return cls.update().values(kwargs).where(cls.id == _id)
        else:
            return cls.insert().add(self)

    @property
    def remove(self) -> Delete:
        """
        A proxy to a delete query.
        """
        _id = self.id

        if type(_id) != int:
            raise ValueError("Can only delete pre-existing rows with an id.")

        self.id = None

        return self.__class__.delete().where(self.__class__.id == _id)

    def get_related(self, column_name: str) -> Objects:
        """
        some_band.get_related('manager')
        """
        cls = self.__class__

        foreign_key = cls.get_column_by_name(column_name)

        if isinstance(foreign_key, ForeignKey):
            references: t.Type[Table] = foreign_key._foreign_key_meta.references

            return (
                references.objects()
                .where(
                    references.get_column_by_name("id")
                    == getattr(self, column_name)
                )
                .first()
            )
        else:
            raise ValueError(f"{column_name} isn't a ForeignKey")

    def __setitem__(self, key: str, value: t.Any):
        setattr(self, key, value)

    def __getitem__(self, key: str):
        return getattr(self, key)

    ###########################################################################

    @property
    def querystring(self) -> QueryString:
        """
        Used when inserting rows.
        """
        args_dict = {
            col._meta.name: self[col._meta.name] for col in self._meta.columns
        }

        is_unquoted = lambda arg: type(arg) == Unquoted

        # Strip out any args which are unquoted.
        # TODO Not the cleanest place to have it (would rather have it handled
        # in the Querystring bundle logic) - might need refactoring.
        filtered_args = [i for i in args_dict.values() if not is_unquoted(i)]

        # If unquoted, dump it straight into the query.
        query = ",".join(
            [
                args_dict[column._meta.name].value
                if is_unquoted(args_dict[column._meta.name])
                else "{}"
                for column in self._meta.columns
            ]
        )
        return QueryString(f"({query})", *filtered_args)

    def __str__(self) -> str:
        return self.querystring.__str__()

    ###########################################################################

    @classmethod
    def get_column_by_name(cls, column_name: str) -> Column:
        columns = [i for i in cls._meta.columns if i._meta.name == column_name]

        if len(columns) != 1:
            raise ValueError(f"Can't find a column called {column_name}.")

        return columns[0]

    @classmethod
    def ref(cls, column_name: str) -> Column:
        """
        Used to get a copy of a column in a reference table.

        Example: manager.name
        """
        local_column_name, reference_column_name = column_name.split(".")

        local_column = cls.get_column_by_name(local_column_name)

        if not isinstance(local_column, ForeignKey):
            raise ValueError(f"{local_column_name} isn't a ForeignKey")

        reference_column = local_column.references.get_column_by_name(
            reference_column_name
        )

        _reference_column = copy.deepcopy(reference_column)
        _reference_column.name = f"{local_column_name}.{reference_column_name}"
        return _reference_column

    ###########################################################################
    # Classmethods

    @classmethod
    # TODO - needs refactoring into Band.insert.rows(some_table_instance)
    def insert(cls, *rows: "Table") -> Insert:
        """
        await Band.insert(
            Band(name="Pythonistas", popularity=500, manager=1)
        ).run()
        """
        query = Insert(table=cls)
        if rows:
            query.add(*rows)
        return query

    @classmethod
    def raw(cls, sql: str) -> Raw:
        """
        await Band.raw('select * from foo')
        """
        return Raw(table=cls, base=QueryString(sql))

    @classmethod
    def select(cls) -> Select:
        """
        Get data.

        await Band.select().columns(Band.name).run()
        """
        return Select(table=cls)

    @classmethod
    def delete(cls) -> Delete:
        """
        await Band.delete().where(Band.name == 'CSharps').run()
        """
        return Delete(table=cls)

    @classmethod
    def create(cls) -> Create:
        """
        Create table, along with all columns.

        await Band.create().run()
        """
        return Create(table=cls)

    @classmethod
    def create_without_columns(cls) -> Raw:
        """
        Create the table, but with no columns (useful for migrations).

        await Band.create().run()
        """
        return Raw(table=cls, base=f'CREATE TABLE "{cls._meta.tablename}"()')

    @classmethod
    def drop(cls) -> Drop:
        """
        Drops the table.

        await Band.drop().run()
        """
        return Drop(table=cls)

    @classmethod
    def alter(cls) -> Alter:
        """
        await Band.alter().rename_column(Band.popularity, 'rating')
        """
        return Alter(table=cls)

    @classmethod
    def objects(cls) -> Objects:
        return Objects(table=cls)

    @classmethod
    def count(cls) -> Count:
        """
        Count the number of matching rows.
        """
        return Count(table=cls)

    @classmethod
    def exists(cls) -> Exists:
        """
        Use it to check if a row exists, not if the table exists.
        """
        return Exists(table=cls)

    @classmethod
    def table_exists(cls) -> TableExists:
        return TableExists(table=cls)

    @classmethod
    def update(cls) -> Update:
        """
        Update rows.

        await Band.update().values(
            {Band.name: "Spamalot"}
        ).where(Band.name=="Pythonistas")
        """
        return Update(table=cls)

