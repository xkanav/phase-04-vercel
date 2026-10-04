"""Small, strict validator for the closed set of Phase 4 tool schemas."""
import re

class Invalid(ValueError):
    pass

def validate(value, schema, path='arguments'):
    if 'anyOf' in schema:
        for item in schema['anyOf']:
            try:
                validate(value, item, path)
                return
            except Invalid:
                pass
        raise Invalid(path + ': invalid value')
    kind = schema.get('type')
    types = {'object':dict, 'string':str, 'array':list, 'integer':int, 'number':(int,float), 'boolean':bool, 'null':type(None)}
    if kind and (not isinstance(value, types[kind]) or kind in ('integer','number') and isinstance(value,bool)):
        raise Invalid(path + ': wrong type')
    if 'enum' in schema and value not in schema['enum']:
        raise Invalid(path + ': unsupported choice')
    if isinstance(value,dict):
        props = schema.get('properties',{})
        if set(schema.get('required',[])) - set(value):
            raise Invalid(path + ': required field missing')
        if schema.get('additionalProperties') is False and set(value)-set(props):
            raise Invalid(path + ': unexpected field')
        for k,v in value.items():
            if k in props: validate(v,props[k],path+'.'+k)
    if isinstance(value,str):
        if len(value)<schema.get('minLength',0) or len(value)>schema.get('maxLength',100000):
            raise Invalid(path + ': invalid length')
        if schema.get('pattern') and not re.search(schema['pattern'],value):
            raise Invalid(path + ': invalid format')
    if type(value) in (int,float):
        if value<schema.get('minimum',float('-inf')) or value>schema.get('maximum',float('inf')):
            raise Invalid(path + ': out of range')
    if isinstance(value,list):
        if len(value)<schema.get('minItems',0): raise Invalid(path+': empty list')
        if schema.get('uniqueItems') and len(value)!=len(set(map(str,value))): raise Invalid(path+': duplicate items')
        for item in value: validate(item,schema.get('items',{}),path+'[]')

def gemini_schema(schema):
    """Translate canonical JSON schema to Gemini's supported OpenAPI subset.

    All constraints, including stripped provider keywords, remain enforced locally.
    """
    if 'anyOf' in schema:
        non_null = [s for s in schema['anyOf'] if s.get('type')!='null']
        result = gemini_schema(non_null[0])
        result['nullable'] = True
        return result
    kind = schema.get('type','string' if 'enum' in schema else 'object')
    result = {'type':kind.upper()}
    for key in ('description','enum','required','minimum','maximum'):
        if key in schema: result[key]=schema[key]
    if 'properties' in schema: result['properties']={k:gemini_schema(v) for k,v in schema['properties'].items()}
    if 'items' in schema: result['items']=gemini_schema(schema['items'])
    return result
