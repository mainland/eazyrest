# Model Reference

```{eval-rst}
.. autoexception:: eazyrest.EazyrestError
```

```{eval-rst}
.. autoexception:: eazyrest.DoesNotExist
```

```{eval-rst}
.. autoexception:: eazyrest.MultipleObjectsReturned
```

```{eval-rst}
.. autoexception:: eazyrest.PrefetchNotSupported
```

```{eval-rst}
.. autoexception:: eazyrest.FieldConversionError
   :members:
```

```{eval-rst}
.. autofunction:: eazyrest.json_object
```

```{eval-rst}
.. autoclass:: eazyrest.Conversion
   :members:
```

```{eval-rst}
.. data:: eazyrest.DEFAULT_CONVERSIONS
   :type: Mapping[type, Conversion]

   eazyrest's conversions for ``datetime``, ``date``, ``timedelta``,
   ``UUID``, and enums, keyed by the type they convert. The mapping is
   read-only. A model's ``conversions`` are merged over it.
```

```{eval-rst}
.. autoclass:: eazyrest.JSONObject
   :members:
   :show-inheritance:
```

```{eval-rst}
.. autodata:: eazyrest.WriteMode
```
