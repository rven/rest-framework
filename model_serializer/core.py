from odoo import _, models
from odoo.exceptions import ValidationError

from odoo.addons.datamodel.core import Datamodel, MetaDatamodel

from .field_converter import convert_field


class class_or_instancemethod(classmethod):  # pylint: disable=class-camelcase
    def __get__(self, instance, type_):
        descr_get = super().__get__ if instance is None else self.__func__.__get__
        return descr_get(instance, type_)


class MetaModelSerializer(MetaDatamodel):
    def __init__(self, name, bases, attrs):
        super(MetaModelSerializer, self).__init__(name, bases, attrs)


class ModelSerializer(Datamodel, metaclass=MetaModelSerializer):
    _inherit = "base"
    _register = False
    _model = None
    _model_fields = []

    @classmethod
    def _check_nested_class(cls, marshmallow_field, registry):
        """If `marshmallow_field` is a nested datamodel (relational field), we check
        if the nested datamodel class exists
        """
        nested_name = getattr(marshmallow_field, "datamodel_name", None)
        if nested_name and nested_name not in registry:
            raise ValidationError(
                _("'{}' datamodel does not exist").format(nested_name)
            )

    @classmethod
    def _extend_from_odoo_model(cls, registry, env):
        """Extend the datamodel to contain the declared Odoo model fields"""
        attrs = {
            "_inherit": cls._name,
            "_model_fields": getattr(cls, "_model_fields", []),
            "_model": getattr(cls, "_model", None),
        }
        bases = (ModelSerializer,)
        name = cls.__name__ + "Child"
        parent_class = registry[cls._name]
        has_model_fields = bool(attrs["_model_fields"])
        if getattr(parent_class, "_model_fields", None):
            has_model_fields = True
        if getattr(parent_class, "_model", None):
            if attrs["_model"] and attrs["_model"] != parent_class._model:
                raise ValidationError(
                    _(
                        "Error in {}: Model Serializers cannot inherit "
                        "from a class having a different '_model' attribute"
                    ).format(cls.__name__)
                )
            attrs["_model"] = parent_class._model

        if not (attrs["_model"] and has_model_fields):
            raise ValidationError(
                _(
                    "Error in {}: Model Serializers require '_model' and "
                    "'_model_fields' attributes to be defined"
                ).format(cls.__name__)
            )

        odoo_model = env[attrs["_model"]]
        for field_name in cls._model_fields:
            if not hasattr(cls, field_name):
                odoo_field = odoo_model._fields[field_name]
                marshmallow_field = convert_field(odoo_field)
                if marshmallow_field:
                    attrs[field_name] = marshmallow_field
            else:
                marshmallow_field = getattr(cls.__schema_class__, field_name)
            cls._check_nested_class(marshmallow_field, registry)
        return MetaDatamodel(name, bases, attrs)

    @property
    def _model_name(self):
        if self.context.get("odoo_model"):
            return self.context["odoo_model"]
        return self._model

    @_model_name.setter
    def _model_name(self, value):
        if not self.context:
            self.context = {}
        self.context["odoo_model"] = value

    @classmethod
    def from_recordset(cls, recordset, *, many=False):
        """Transform a recordset into a (list of) datamodel(s)"""

        def convert_null_value(val):
            if val:
                return val
            if val is False or isinstance(val, models.BaseModel):
                return None
            return val

        res = []
        datamodels = recordset.env.datamodels
        recordset = recordset if many else recordset[:1]
        for record in recordset:
            instance = cls(partial=True, context={"odoo_model": record._name})
            for model_field in cls._model_fields:
                schema_field = instance.__schema__.fields[model_field]
                nested_datamodel_name = getattr(schema_field, "datamodel_name", None)
                if nested_datamodel_name and record[model_field]:
                    nested_datamodel_class = datamodels[nested_datamodel_name]
                    if hasattr(nested_datamodel_class, "from_recordset"):
                        setattr(
                            instance,
                            model_field,
                            nested_datamodel_class.from_recordset(
                                record[model_field], many=schema_field.many
                            ),
                        )
                else:
                    value = convert_null_value(record[model_field])
                    setattr(instance, model_field, value)
            res.append(instance)
        if res and not many:
            return res[0]
        return res

    def get_odoo_record(self):
        """Get an existing record matching `self`. Meant to be overridden
        TODO: optimize this to deal with multiple instances at once
        """
        odoo_model = self.env[self._model_name]
        if "id" in self._model_fields and getattr(self, "id", None):
            return odoo_model.browse(self.id)
        return self._new_odoo_record()

    def _new_odoo_record(self):
        odoo_model = self.env[self._model_name]
        default_values = odoo_model.default_get(odoo_model._fields.keys())
        return odoo_model.new(default_values)

    def _process_model_value(self, value, model_field):
        if hasattr(self, "validate_{}".format(model_field)):
            return getattr(self, "validate_{}".format(model_field))(value)
        return value

    @classmethod
    def _many_to_recordset(cls, instances, create=True, start=None):
        """Transform `instances` into a corresponding recordset

        :param instances: datamodels to transform
        :param create: whether to create new records or keep them in memory
        :param start: if instances is empty, or if the serializer is generic (used
                      for different models), allows to determine the target Odoo
                      model
        :return: a recordset
        """
        if not instances:
            return start if isinstance(start, models.BaseModel) else []
        env = instances[0].env
        recordset = (
            start
            if isinstance(start, models.BaseModel)
            else env[instances[0]._model].browse([])
        )
        model_name = recordset._name
        for instance in instances:
            instance._model_name = model_name
            record = instance.get_odoo_record() or instance._new_odoo_record()
            # in case of partial, not all fields are considered
            self_fields = instance.dump().keys()
            model_fields = set(self_fields) & set(instance._model_fields)
            for model_field in model_fields:
                schema_field = instance.__schema__.fields[model_field]
                if schema_field.dump_only:
                    continue
                value = getattr(instance, model_field)
                nested_datamodel_name = getattr(schema_field, "datamodel_name", None)
                if nested_datamodel_name:
                    comodel = instance.env[record._fields[model_field].comodel_name]
                    nested_instances = value if isinstance(value, list) else [value]
                    nested_start = comodel.browse([])
                    value = env.datamodels[nested_datamodel_name]._many_to_recordset(
                        nested_instances, create=False, start=nested_start
                    )
                record[model_field] = instance._process_model_value(value, model_field)
            if create and isinstance(record.id, models.NewId):
                values = {
                    field_name: record[field_name] for field_name in record._cache
                }
                values = record._convert_to_write(values)
                record = record.create(values)
            recordset += record
        return record

    @class_or_instancemethod
    def to_recordset(self, *, instances=None, create=True, start=None):
        if instances is None and isinstance(self, ModelSerializer):
            instances = [self]
        return self._many_to_recordset(instances, create=create, start=start)
