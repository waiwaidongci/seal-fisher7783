"""测试专用的最小 pydantic 垫片，仅实现 schemas/rewind.py 使用的能力。"""
import typing


class FieldInfo:
    def __init__(self, default, **kwargs):
        self.default = default
        self.kwargs = kwargs


def Field(default=None, **kwargs):
    return FieldInfo(default, **kwargs)


class ValidationError(Exception):
    pass


class BaseModel:
    def __init__(self, **kwargs):
        annotations = {}
        for klass in reversed(type(self).__mro__):
            annotations.update(getattr(klass, '__annotations__', {}))
        errors = []
        for name, annotation in annotations.items():
            info = getattr(type(self), name, None)
            required = isinstance(info, FieldInfo) and info.default is ...
            default = info.default if isinstance(info, FieldInfo) else info
            if name not in kwargs:
                if required:
                    errors.append(f'{name} 为必填项')
                    continue
                value = default
            else:
                value = kwargs[name]
            value = self._check(name, value, annotation, info, errors)
            setattr(self, name, value)
        unknown = set(kwargs) - set(annotations)
        if unknown:
            errors.append('未知字段: ' + ', '.join(sorted(unknown)))
        if errors:
            raise ValidationError('; '.join(errors))

    @staticmethod
    def _check(name, value, annotation, info, errors):
        origin = typing.get_origin(annotation)
        if origin in (list, typing.List):
            if not isinstance(value, list):
                errors.append(f'{name} 必须是列表')
                return value
            item_type = typing.get_args(annotation)[0]
            if isinstance(item_type, type) and issubclass(item_type, BaseModel):
                return [item_type(**item) if isinstance(item, dict) else item for item in value]
            return value
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            if isinstance(value, dict):
                return annotation(**value)
            if not isinstance(value, annotation):
                errors.append(f'{name} 类型不正确')
            return value
        # 标量
        if origin in (typing.Union,):
            args = [a for a in typing.get_args(annotation) if a is not type(None)]
            if value is None:
                return None
            annotation = args[0]
        if annotation is int:
            if isinstance(value, bool) or not isinstance(value, int):
                errors.append(f'{name} 必须是整数')
        elif annotation is str:
            if not isinstance(value, str):
                errors.append(f'{name} 必须是字符串')
            else:
                max_length = getattr(info, 'kwargs', {}).get('max_length')
                min_length = getattr(info, 'kwargs', {}).get('min_length')
                if max_length and len(value) > max_length:
                    errors.append(f'{name} 长度超限')
                if min_length and len(value) < min_length:
                    errors.append(f'{name} 长度不足')
        return value

    def model_dump(self):
        out = {}
        for name in getattr(type(self), '__annotations__', {}):
            value = getattr(self, name)
            if isinstance(value, BaseModel):
                out[name] = value.model_dump()
            elif isinstance(value, list) and value and isinstance(value[0], BaseModel):
                out[name] = [item.model_dump() for item in value]
            else:
                out[name] = value
        return out
