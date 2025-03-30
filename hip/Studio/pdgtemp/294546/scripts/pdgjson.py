#
# Copyright (c) <2020> Side Effects Software Inc.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# NAME:	        pdgjson.py ( Python )
#
# COMMENTS:     Job side API, to provide access to work item attributes and data
#               as if the work item was in a Houdini process.
#

from __future__ import print_function, absolute_import

import base64
import json
import gzip
import sys
import os

try:
    import pdgcmd
    from pdgcmd import localizePath
except:
    try:
        import pdgjob.pdgcmd as pdgcmd
        from pdgjob.pdgcmd import localizePath
    except ImportError:
        localizePath = lambda f: f

def getTempDir():
    try:
        return os.environ['PDG_TEMP']
    except KeyError:
        pdgcmd.printlog("ERROR: $PDG_TEMP must be in environment")
        return None

def getWorkItemJsonPath(item_name):
    temp_dir = getTempDir()
    if not temp_dir:
        return None

    json_root = os.path.expandvars(temp_dir + '/data/' + item_name)
    if os.path.exists(json_root + '.json.gz'):
        return json_root + '.json.gz'

    return json_root + '.json'

def getPyAttribLoader():
    try:
        return os.environ['PDG_PYATTRIB_LOADER']
    except KeyError:
        return ""

def getWorkItemDataSource():
    try:
        return int(os.environ['PDG_ITEM_DATA_SOURCE'])
    except KeyError:
        return 0

def getMemoryUsage():
    try:
        import psutil
        proc = psutil.Process()
        mem_info = proc.memory_info()
        return mem_info.rss
    except:
        return 0

def isIterable(obj):
    if sys.version_info.major >= 3:
        from collections.abc import Iterable
        is_iterable = isinstance(obj, Iterable) \
            and not isinstance(obj, (str, bytes, bytearray, dict))
    else:
        is_iterable = isinstance(obj, (list, tuple))
    return is_iterable

class _enum(object):
    @classmethod
    def _preloadEnums(cls):
        for index in cls._names:
            name = cls._names[index]
            setattr(cls, name, cls(index))

    def __init__(self, value, prefix):
        self._value = value
        if value in self._names:
            self._name = "{}.{}".format(prefix, self._names[value])
        else:
            self._name = "{}.???".format(prefix)

    def __int__(self):
        return self._value

    def __repr__(self):
        return self._name

    def __str__(self):
        return self._name

    def __hash__(self):
        return self._value

    def __eq__(self, other):
        return self._value == int(other)

    def __ne__(self, other):
        return self._value != int(other)

class attribType(_enum):
    _names = {-1: "Undefined", 0: "Int", 1: "Float", 2: "String", 3: "File",
              4: "PyObject", 5: "Geometry", 6: "Dict"}

    def __init__(self, value):
        _enum.__init__(self, value, "attribType")

attribType._preloadEnums()

class attribFlag(_enum):
    _names = {0: "NoFlags", 0xFFFF: "All", 0x0001: "NoCopy", 0x0002: "EnvExport",
              0x0004: "ReadOnly", 0x0008: "Internal", 0x0010: "Operator"}

    def __init__(self, value):
        _enum.__init__(self, value, "attribFlag")

attribFlag._preloadEnums()

class fileType(_enum):
    _names = {0: "Generated", 1: "Expected", 2: "Runtime", 3: "RuntimeExpected"}

    def __init__(self, value):
        _enum.__init__(self, value, "fileType")

fileType._preloadEnums()

class File(object):
    DataKey     = "data"
    HashKey     = "hash"
    OwnKey      = "own"
    SizeKey     = "size"
    TagKey      = "tag"
    TypeKey     = "type"

    def __init__(self, path="", tag="", modtime=0, owned=False):
        self.path = path
        self.local_path = path
        self.tag = tag
        self.hash = modtime
        self.owned = owned
        self.type = fileType.Runtime

    def __repr__(self):
        return "<{} tag='{}', owned={} at 0x{:x}>".format(self.path, self.tag,
            self.owned, id(self))

    def asTuple(self):
        return (self.path, self.tag, self.hash, self.owned)

    def asJSONValue(self):
        return {
            self.DataKey:   self.path,
            self.TagKey:    self.tag,
            self.HashKey:   self.hash,
            self.SizeKey:   0,
            self.OwnKey:    self.owned,
            self.TypeKey:   int(self.type),
        }

class AttribError(Exception):
    pass

class CookError(Exception):
    pass

class AttributeBase(object):
    BuiltinAttribPrefix = "__pdg";

    InputFilesName      = "__pdg_inputfiles";
    OutputFilesName     = "__pdg_outputfiles";
    AddedFilesName      = "__pdg_addedfiles";

    CommandStringName   = "__pdg_commandstring";
    LabelName           = "__pdg_label";
    CustomStateName     = "__pdg_state";
    CookPercentName     = "__pdg_cookpercent";

    ConcatKey           = "concat"
    FlagKey             = "flag"
    OwnKey              = "own"
    TypeKey             = "type"
    ValueKey            = "value"

    def __init__(self, attrib_type, name, owner):
        self._attrib_type = attrib_type
        self._attrib_flags = 0
        self._owner = owner
        self._name = name
        self._initialized = False
        self._changed = False

    def asJSON(self):
        return {
            self.TypeKey:   int(self._attrib_type),
            self.FlagKey:   int(self._attrib_flags),
            self.OwnKey:    True,
            self.ConcatKey: False,
            self.ValueKey:  self.asJSONValue()
        }

    @property
    def owner(self):
        return self._owner

    @property
    def type(self):
        return self._attrib_type

    @property
    def size(self):
        return len(self)

    @property
    def flags(self):
        return self._attrib_flags

    @property
    def changed(self):
        return self._changed

    @property
    def builtin(self):
        return self._name.startswith(self.BuiltinAttribPrefix)

    def hasFlag(self, flag):
        int_flag = int(flag)
        return (self._attrib_flags & int_flag)

    def hasFlags(self, flags):
        int_flags = int(flags)
        return (self._attrib_flags & int_flags) == int_flags

    def hasAnyFlags(self):
        return self._attrib_flags != 0

    def setFlag(self, flag):
        int_flag = int(flag)
        self._attrib_flags |= int_flag

    def setFlags(self, flags):
        self._attrib_flags = flags

    def asNumber(self, index=0):
        return 0

    def asString(self, index=0):
        return ""

    def _updateValue(self):
        if not self._initialized:
            return False

        self._changed = True
        if not self._owner.shouldReportChanges or self.builtin:
            return False

        return True

class AttributeArray(AttributeBase):
    def __init__(self, attrib_type, data_type, name, owner):
        AttributeBase.__init__(self, attrib_type, name, owner)
        self._data_type = data_type
        self._data = []

    def __len__(self):
        return len(self._data)

    def __getitem__(self, index):
        return self._data[index]

    def __setitem__(self, index, value):
        self.setValue(value, index)

    def __hash__(self):
        h = 0
        for v in self._data:
            h ^= hash(v)
        return h

    def asJSONValue(self):
        return self._data

    @property
    def values(self):
        return self._data

    def value(self, index=0):
        if index < 0:
            index += len(self._data)

        return self._data[index]

    def setValue(self, value, index=0):
        _value = self._cast(value)

        length = len(self._data)
        if index < 0:
            index = length + index + 1

        if index >= length:
            self._data += [self._data_type()]*(index-length+1)

        self._data[index] = _value
        if self._updateValue():
            self._reportValue(_value, index)

    def setValues(self, values):
        self._data = [self._cast(val) for val in values]
        if self._updateValue():
            self._reportArray(self._data)

    def truncate(self, length):
        self._data = self._data[0:length]
        if self._updateValue():
            self._reportArray(self._data)

    def _cast(self, value):
        return self._data_type(value)

    def _reportValue(self, value, index):
        pass

    def _reportArray(self, values):
        pass

class AttributeInt(AttributeArray):
    def __init__(self, name, owner):
        AttributeArray.__init__(self, attribType.Int, int, name, owner)

    def _reportValue(self, value, index):
        pdgcmd.setIntAttrib(self._name, value, index, self._owner.reportId,
                            self._owner.batchIndex, self._owner._server)

    def _reportArray(self, values):
        pdgcmd.setIntAttribArray(self._name, values, self._owner.reportId,
                                 self._owner.batchIndex, self._owner._server)

class AttributeFloat(AttributeArray):
    def __init__(self, name, owner):
        AttributeArray.__init__(self, attribType.Float, float, name, owner)

    def _reportValue(self, value, index):
        pdgcmd.setFloatAttrib(self._name, value, index, self._owner.reportId,
                              self._owner.batchIndex, self._owner._server)

    def _reportArray(self, values):
        pdgcmd.setFloatAttribArray(self._name, values, self._owner.reportId,
                                   self._owner.batchIndex, self._owner._server)

class AttributeString(AttributeArray):
    def __init__(self, name, owner):
        AttributeArray.__init__(self, attribType.String, str, name, owner)

    def _reportValue(self, value, index):
        pdgcmd.setStringAttrib(self._name, value, index, self._owner.reportId,
                               self._owner.batchIndex, self._owner._server)

    def _reportArray(self, values):
        pdgcmd.setStringAttribArray(self._name, values, self._owner.reportId,
                                    self._owner.batchIndex, self._owner._server)

    def _cast(self, value):
        if isinstance(value, self._data_type):
            return value

        try:
            return self._data_type(value, 'utf8')
        except TypeError:
            return AttributeArray._cast(self, value)

class AttributeFile(AttributeArray):
    def __init__(self, name, owner):
        AttributeArray.__init__(self, attribType.File, File, name, owner)

    def _reportValue(self, value, index):
        pdgcmd.setFileAttrib(self._name, value.asTuple(), index,
                             self._owner.reportId, self._owner.batchIndex,
                             self._owner._server)

    def _reportArray(self, values):
        pdgcmd.setFileAttribArray(self._name, [value.asTuple() for value in values],
                                  self._owner.reportId, self._owner.batchIndex,
                                  self._owner._server)
    def asJSONValue(self):
        json_values = []
        for value in self._data:
            json_values.append(value.asJSONValue())
        return json_values

    def setValue(self, value, index=0):
        if not isinstance(value, self._data_type):
            raise AttribError("File attributes must be set to pdg.File values")

        length = len(self._data)
        if index < 0:
            index = length + index + 1

        if index >= length:
            self._data += [self._data_type()]*(index-length+1)

        self._data[index] = value
        if self._updateValue():
            self._reportValue(value, index)

    def setValues(self, values):
        for val in values:
            if not isinstance(val, self._data_type):
                raise AttribError("File attributes must be set to pdg.File values")

        self._data = values
        if self._updateValue():
            self._reportArray(values)

    def _queryValues(self, value_type):
        if value_type == 0:
            return self.values
        elif value_type == 1:
            return [v for v in self.values if v.type != fileType.Expected]
        elif value_type == 2:
            return [v for v in self.values if v.type == fileType.Expected]

class AttributeDictionary(AttributeArray):
    def __init__(self, name, owner):
        AttributeArray.__init__(self, attribType.Dict, dict, name, owner)

    def _reportValue(self, value, index):
        str_repr = str(value)
        pdgcmd.setDictAttrib(self._name, str_repr, index, self._owner.reportId,
                              self._owner.batchIndex, self._owner._server)

    def _reportArray(self, values):
        str_repr = [str(value) for value in values]
        pdgcmd.setFloatAttribArray(self._name, str_repr, self._owner.reportId,
                                   self._owner.batchIndex, self._owner._server)

class AttributePyObject(AttributeBase):
    def __init__(self, name, owner):
        AttributeBase.__init__(self, attribType.PyObject, name, owner)
        self._object = None

    def __len__(self):
        return 1

    def __hash__(self):
        try:
            return hash(self._object)
        except TypeError:
            return hash(str(self._object))
        return 0

    def asJSONValue(self):
        loader = getPyAttribLoader()
        if not loader:
            obj_repr = repr(self._object)
        else:
            mod = __import__(loader)
            obj_repr = base64.encodebytes(mod.dumps(self._object).encode('utf-8'))
        return obj_repr
   
    @property
    def object(self):
        return self._object

    @object.setter
    def object(self, obj):
        self._object = obj
        if self._updateValue():
            self._reportValue(obj, 0)

    @property
    def values(self):
        return self._object

    def value(self, index=0):
        return self._object

    def setValue(self, value, index=0):
        self._object = value
        if self._updateValue():
            self._reportValue(value, 0)

    def _reportValue(self, value, index):
        if not self._initialized:
            return

        obj_repr = self.asJSONValue()
        pdgcmd.setPyObjectAttrib(self._name, obj_repr, self._owner.reportId,
                                 self._owner.batchIndex, self._owner._server)

class batchActivation(_enum):
    _names = {0: "First", 1: "Any", 2: "All"}

    def __init__(self, value):
        _enum.__init__(self, value, "batchActivation")

batchActivation._preloadEnums()

class workItemState(_enum):
    _names = {0: "Undefined", 1: "Dirty", 2: "Uncooked", 3: "Waiting",
              4: "Scheduled", 5: "Cooking", 6: "CookedSuccess", 7: "CookedCache",
              8: "CookedFail", 9: "CookedCancel"}

    def __init__(self, value):
        _enum.__init__(self, value, "workItemState")

workItemState._preloadEnums()

class workItemType(_enum):
    _names = {0: "Regular", 1: "Batch", 2: "Partition"}

    def __init__(self, value):
        _enum.__init__(self, value, "workItemType")

workItemType._preloadEnums()

class workItemExecutionType(_enum):
    _names = {0: "Regular", 1: "LongRunning", 2: "Cleanup"}

    def __init__(self, value):
        _enum.__init__(self, value, "workItemExecutionType")

workItemExecutionType._preloadEnums()

class workItemLogType(_enum):
    _names = {0: "Error", 1: "Warning", 2: "Message", 3: "Raw"}

    def __init__(self, value):
        _enum.__init__(self, value, "workItemLogType")

workItemLogType._preloadEnums()

class workItemCookType(_enum):
    _names = {0: "Generate", 1: "InProcess", 2: "OutOfProcess", 3: "Service",
              4: "Automatic"}

    def __init__(self, value):
        _enum.__init__(self, value, "workItemCookType")

workItemCookType._preloadEnums()

class AttributeOwner(object):
    _types = {-1: None, 0: AttributeInt, 1: AttributeFloat, 2: AttributeString,
              3: AttributeFile, 4: AttributePyObject, 5: None,
              6: AttributeDictionary}

    def __init__(self, expand_strings):
        self._expandStrings = expand_strings
        self._attributes = {}

    def __getitem__(self, key):
        return self._attributes[key]

    @property
    def shouldReportChanges(self):
        return False

    def attrib(self, key, attrib_type=attribType.Undefined, initialized=True):
        attrib = self._attributes.get(key)
        if attrib is None:
            return None
        if attrib_type == attribType.Undefined:
            return attrib
        if attrib.type == attrib_type:
            return attrib
        return None

    def attribHash(self, include_internal=True):
        for attrib_name, v in self._attributes.items():
            if not include_internal and v.hasFlag(attribFlag.Internal):
                continue 
            h ^= hash(attrib_name)
            h ^= hash(v)
            h ^= hash(v.flags)
        return h

    def attribNames(self, attrib_type=attribType.Undefined):
        if attrib_type == attribType.Undefined:
            return list(self._attributes.keys())
        else:
            names = []
            for attrib in self._attributes:
                if attrib.type == attrib_type:
                    names.append(attrib)
            return names

    def attribValues(self, attrib_type=attribType.Undefined):
        d = {}

        for key in self._attributes:
            if (attrib_type == attribType.Undefined) or \
                    (attrib_type == self._attributes[key].type):
                d[key] = self._attributes[key].values
        return d

    def addAttrib(self, key, attrib_type, initialized=True):
        attrib = self.attrib(key, initialized=initialized)
        if attrib is None:
            int_type = int(attrib_type)
            if int_type not in self._types:
                raise AttribError("Unsupported attribute type")
            if self._types[int_type] is None:
                raise AttribError("Unsupported attribute type")

            attrib = self._types[int_type](key, self)
            attrib._initialized = initialized
            self._attributes[key] = attrib
            return attrib

        if attrib.type != attrib_type:
            raise AttribError("An attribute with the name '{}' already exists\
                with a different type".format(key))
        return attrib

    def hasAttrib(self, key):
        return key in self._attributes

    def attribValue(self, name, index=0):
        return self._attribValue(name, index, attribType.Undefined)

    def intAttribValue(self, name, index=0):
        return self._attribValue(name, index, attribType.Int)

    def floatAttribValue(self, name, index=0):
        return self._attribValue(name, index, attribType.Float)

    def stringAttribValue(self, name, index=0):
        return self._attribValue(name, index, attribType.String)

    def fileAttribValue(self, name, index=0):
        return self._attribValue(name, index, attribType.File)

    def dictAttribValue(self, name, index=0):
        return self._attribValue(name, index, attribType.Dict)

    def pyObjectAttribValue(self, name):
        return self._attribValue(name, 0, attribType.PyObject)

    def attribArray(self, name):
        return self._attribValues(name, attribType.Undefined)

    def intAttribArray(self, name):
        return self._attribValues(name, attribType.Int)

    def floatAttribArray(self, name):
        return self._attribValues(name, attribType.Float)

    def stringAttribArray(self, name):
        return self._attribValues(name, attribType.String)

    def fileAttribArray(self, name):
        return self._attribValues(name, attribType.File)

    def dictAttribArray(self, name):
        return self._attribValues(name, attribType.Dict)

    def setIntAttrib(self, name, value, index=0):
        self.addAttrib(name, attribType.Int)

        if isIterable(value):
            return self._setAttribArray(name, value, attribType.Int)

        return self._setAttrib(name, index, value, attribType.Int)

    def setFloatAttrib(self, name, value, index=0):
        self.addAttrib(name, attribType.Float)

        if isIterable(value):
            return self._setAttribArray(name, value, attribType.Float)

        return self._setAttrib(name, index, value, attribType.Float)

    def setStringAttrib(self, name, value, index=0):
        self.addAttrib(name, attribType.String)

        if isIterable(value):
            return self._setAttribArray(name, value, attribType.String)

        return self._setAttrib(name, index, value, attribType.String)

    def setFileAttrib(self, name, value, index=0):
        self.addAttrib(name, attribType.File)

        if isIterable(value):
            return self._setAttribArray(name, value, attribType.File)

        return self._setAttrib(name, index, value, attribType.File)

    def setDictAttrib(self, name, value, index=0):
        self.addAttrib(name, attribType.Dict)

        if isIterable(value):
            return self._setAttribArray(name, value, attribType.Dict)

        return self._setAttrib(name, index, value, attribType.Dict)

    def setPyObjectAttrib(self, name, value, index=0):
        self.addAttrib(name, attribType.PyObject)
        return self._setAttrib(name, index, value, attribType.PyObject)

    def _values(self, flags):
        results = {}

        for attrib_name, v in self._attributes.items():
            if v.hasFlag(attribFlag.Internal):
                continue 
            if not v.hasFlags(flags):
                continue
            results[attrib_name] = v.value(0)

        return results

    def _attribValue(self, name, index, attrib_type):
        attr = self.attrib(name, attrib_type)
        if attr is None:
            return None
        return attr.value(index)

    def _attribValues(self, name, attrib_type):
        attr = self.attrib(name, attrib_type)
        if attr is None:
            return None
        return attr.values

    def _setAttrib(self, name, index, value, attrib_type):
        attr = self.attrib(name, attrib_type)
        if attr is None:
            return False
        attr.setValue(value, index)
        return True

    def _setAttribArray(self, name, value, attrib_type):
        attr = self.attrib(name, attrib_type)
        if attr is None:
            return False
        attr.setValues(value)
        return True

    def _loadAttributes(self, json_obj):
        attribs = json_obj['attributes']

        for attrib_name in attribs:
            attrib = attribs[attrib_name]

            typenum = attrib[AttributeBase.TypeKey]
            if typenum is not None:
                attrib_type = attribType(typenum)
                if attrib_type == attribType.Geometry:
                    continue

                new_attr = self.addAttrib(attrib_name, attrib_type, False)
                new_attr.setFlags(attrib[AttributeBase.FlagKey])

                raw_value = attrib[AttributeBase.ValueKey]

                if attrib_type == attribType.PyObject:
                    try:
                        loader = getPyAttribLoader()
                        if not loader:
                            new_attr.object = eval(raw_value)
                        else:
                            mod = __import__(loader)
                            new_attr.object = mod.loads(base64.b64decode(raw_value))
                    except:
                        new_attr.object = None
                        pdgcmd.printlog(
                            "ERROR: Python object for attribute '{}' "
                            "failed to deserialize.".format(attrib_name))
                        import traceback
                        traceback.print_exc()
                        continue
                elif attrib_type == attribType.File:
                    new_files = []
                    for val in raw_value:
                        f = File()
                        f.path = val['data']
                        f.local_path = localizePath(val['data'])
                        f.tag = val['tag']
                        f.hash = val['hash']
                        f.owned = val['own']
                        f.type = fileType(val['type'])
                        new_files.append(f)
                    new_attr.setValues(new_files)
                elif attrib_type == attribType.String:
                    if self._expandStrings:
                        new_attr.setValues([localizePath(val) for val in raw_value])
                    else:
                        new_attr.setValues(raw_value)
                else:
                    new_attr.setValues(raw_value)
                new_attr._initialized = True

class Graph(AttributeOwner):
    def __init__(self, expand_strings):
        AttributeOwner.__init__(self, expand_strings)

class NativeWorkItem(object):
    def __init__(self, work_item, batch_parent=None, batch_index=-1, server=None):
        self.__dict__["_batchIndex"] = batch_index
        self.__dict__["_batchParent"] = batch_parent
        self.__dict__["_server"] = os.environ.get('PDG_RESULT_SERVER', server)
        self.__dict__["_tempDir"] = getTempDir()

        if work_item:
            self.__dict__["_workItem"] = work_item
            self.__dict__["_stub"] = False

            if work_item.isBatch:
                self.__dict__["_subItems"] = [None]*work_item.batchSize

                last = -1
                for i, sub_item in enumerate(work_item.batchItems):
                    self._subItems[i] = NativeWorkItem(
                        sub_item, self, i, self._server)
                    last = i

                for remainder in range(last+1, work_item.batchSize):
                    self._subItems[remainder] = NativeWorkItem(
                        None, self, remainder, self._server)
        else:
            self.__dict__["_workItem"] = None
            self.__dict__["_stub"] = True

    def __getitem__(self, key):
        return self._workItem.__getitem__(key)

    def __dir__(self):
        dir_list = list(self.__dict__.keys())
        for entry in dir(self.__class__):
            if not entry.startswith("__"):
                dir_list.append(entry)

        for entry in dir(self._workItem):
            if not entry.startswith("__"):
                dir_list.append(entry)

        dir_list.sort()
        return dir_list

    def __getattr__(self, attr_name):
        # never want to see the repr() of the wrapped object
        if attr_name == '__repr__':
            raise AttributeError

        try:
            return getattr(self._workItem, attr_name)
        except:
            error_str = "'{}' object has no attribute '{}'".format(
                    self.__class__.__name__, attr_name)
            raise AttributeError(error_str)

    def __setattr__(self, attr_name, attr_val):
        if hasattr(self._workItem, attr_name):
            setattr(self._workItem, attr_name, attr_val)
        self.__dict__[attr_name] = attr_val

    @property
    def batchItems(self):
        return self._subItems

    @property
    def reportId(self):
        if self._batchParent:
            return self._batchParent.id
        return self.id

    @property
    def tempDir(self):
        return self._tempDir

    def setJobState(self, state):
        if state == workItemState.Cooking:
            pdgcmd.workItemStartCook(
                self.reportId, self._batchIndex, self._server)
        elif state == workItemState.CookedSuccess:
            pdgcmd.workItemSuccess(
                self.reportId, self._batchIndex, self._server)
        elif state == workItemState.CookedFail:
            pdgcmd.workItemFailed(
                self.reportId, self._server)
        else:
            raise TypeError("Job can not be set to {} state".format(state))

    @property
    def shouldReportChanges(self):
        return not self.isServiceMode

    @property
    def isServiceMode(self):
        import pdg
        return self.cookType == pdg.workItemCookType.Service

    def setCustomState(self, custom_state):
        if self.shouldReportChanges:
            pdgcmd.workItemSetCustomState(
                custom_state,
                workitem_id=self.reportId,
                subindex=self.batchIndex)

    def setCookPercent(self, cook_percent):
        if self.shouldReportChanges:
            pdgcmd.workItemSetCookPercent(
                cook_percent,
                workitem_id=self.reportId,
                subindex=self.batchIndex)

    def startSubItem(self, wait=False):
        if not self._batchParent:
            raise TypeError(
                "Work item has no batch parent")

        if wait:
            pdgcmd.waitUntilReady(self.reportId, self._batchIndex)

        if self._stub:
            native_item = WorkItem.fromRPC(
                self.reportId, self._batchIndex, self._server, as_native=True)
            self._workItem = native_item._workItem
            self._stub = False

        self._batchParent.setActiveBatchIndex(self._batchIndex)
        self.setJobState(workItemState.Cooking)
        return True

    def checkSubItem(self):
        if not self._batchParent:
            raise TypeError(
                "Work item has no batch parent")

        result = pdgcmd.checkReady(self.reportId, self._batchIndex)

        if result and self._stub:
            native_item = WorkItem.fromRPC(
                self.reportId, self._batchIndex, self._server, as_native=True)
            self._workItem = native_item._workItem
            self._stub = False

        return result

    def cookSubItem(self, state=workItemState.CookedSuccess, duration=0):
        if not self._batchParent:
            raise TypeError("Work item has no batch parent")
        self.setJobState(state)

    def addError(self, message, fail_task=False, timestamp=True):
        import hou
        pdgcmd.printlog(message, prefix='ERROR', timestamp=timestamp)
        if fail_task:
            hou.exit(-1)

    def addMessage(self, message, verbosity=pdgcmd.LogDefault, timestamp=True):
        pdgcmd.printlog(
            message,
            verbosity=verbosity,
            timestamp=timestamp)

    def addWarning(self, message, verbosity=pdgcmd.LogDefault, timestamp=True):
        pdgcmd.printlog(
            message,
            verbosity=verbosity,
            prefix='WARNING',
            timestamp=timestamp)

    def addOutputFile(self, path, tag="", hash_code=0, own=False):
        self._workItem.addOutputFile(path, tag, hash_code, own)

        if self.shouldReportChanges:
            pdgcmd.addOutputFile(
                path,
                workitem_id=self.reportId,
                subindex=self.batchIndex,
                result_data_tag=tag,
                hash_code=hash_code,
                server_addr=self._server,
                raise_exc=False)

    def addOutputFiles(self, paths, tag="", hash_codes=[], own=False):
        if not paths:
            return

        if len(paths) == 1:
            if not tag:
                tag = ""
            elif isIterable(tag):
                tag = tag[0]

            if len(hash_codes) > 0:
                self.addOutputFile(paths[0], tag, hash_codes[0], own)
            else:
                self.addOutputFile(paths[0], tag, 0, own)
            return

        self._workItem.addOutputFiles(paths, tag, hash_codes, own)

        if self.shouldReportChanges:
            pdgcmd.addOutputFiles(
                paths,
                workitem_id=self.reportId,
                subindex=self.batchIndex,
                output_file_tag=tag,
                hash_codes=hash_codes,
                server_addr=self._server,
                raise_exc=False)

    def setIntAttrib(self, name, value, index=0):
        if isIterable(value):
            self._workItem.setIntAttrib(name, value)
            pdgcmd.setIntAttribArray(
                name,
                value,
                workitem_id=self.reportId,
                subindex=self.batchIndex,
                server_addr=self._server,
                raise_exc=False)
        else:
            self._workItem.setIntAttrib(name, value, index)
            pdgcmd.setIntAttrib(
                name,
                value,
                index,
                workitem_id=self.reportId,
                subindex=self.batchIndex,
                server_addr=self._server,
                raise_exc=False)

    def setFloatAttrib(self, name, value, index=0):
        if isIterable(value):
            self._workItem.setFloatAttrib(name, value)
            pdgcmd.setFloatAttribArray(
                name,
                value,
                workitem_id=self.reportId,
                subindex=self.batchIndex,
                server_addr=self._server,
                raise_exc=False)
        else:
            self._workItem.setFloatAttrib(name, value, index)
            pdgcmd.setFloatAttrib(
                name,
                value,
                index,
                workitem_id=self.reportId,
                subindex=self.batchIndex,
                server_addr=self._server,
                raise_exc=False)

    def setStringAttrib(self, name, value, index=0):
        if isIterable(value):
            self._workItem.setStringAttrib(name, value)
            pdgcmd.setStringAttribArray(
                name,
                value,
                workitem_id=self.reportId,
                subindex=self.batchIndex,
                server_addr=self._server,
                raise_exc=False)
        else:
            self._workItem.setStringAttrib(name, value, index)
            pdgcmd.setStringAttrib(
                name,
                value,
                index,
                workitem_id=self.reportId,
                subindex=self.batchIndex,
                server_addr=self._server,
                raise_exc=False)

    def setFileAttrib(self, name, value, index=0):
        if isIterable(value):
            self._workItem.setFileAttrib(name, value)
            pdgcmd.setFileAttribArray(
                name,
                [x.asTuple() for x in value],
                workitem_id=self.reportId,
                subindex=self.batchIndex,
                server_addr=self._server,
                raise_exc=False)
        else:
            self._workItem.setFileAttrib(name, value, index)
            pdgcmd.setFileAttrib(
                name,
                value.asTuple(),
                index,
                workitem_id=self.reportId,
                subindex=self.batchIndex,
                server_addr=self._server,
                raise_exc=False)

    def setDictAttrib(self, name, value, index=0):
        if isIterable(value):
            self._workItem.setDictAttrib(name, value)
            pdgcmd.setDictAttribArray(
                name,
                [str(x) for x in value],
                workitem_id=self.reportId,
                subindex=self.batchIndex,
                server_addr=self._server,
                raise_exc=False)
        else:
            self._workItem.setDictAttrib(name, value, index)
            pdgcmd.setDictAttrib(
                name,
                str(value),
                index,
                workitem_id=self.reportId,
                subindex=self.batchIndex,
                server_addr=self._server,
                raise_exc=False)

    def setPyObjectAttrib(self, name, value, index=0):
        self._workItem.setPyObjectAttrib(name, value)

        loader = getPyAttribLoader()
        if not loader:
            obj_repr = repr(value)
        else:
            mod = __import__(loader)
            obj_repr = base64.encodebytes(mod.dumps(value).encode('utf-8'))
 
        pdgcmd.setPyObjectAttrib(
            name,
            obj_repr,
            workitem_id=self.reportId,
            subindex=self.batchIndex,
            server_addr=self._server,
            raise_exc=False)

    def localizePath(self, path):
        return localizePath(path)

    @classmethod
    def serviceFailure(cls, msg=''):
        import pdg

        response = {
            'status': 'failed',
            'msg': msg,
            'version': pdg.AttributeInfo.Version,
            'memory' : getMemoryUsage()
        }

        return response

    def serviceResponse(self, status, msg=''):
        import pdg

        response = {
            'status': status,
            'msg': msg,
            'version': pdg.AttributeInfo.Version,
            'memory' : getMemoryUsage()
        }

        if status != 'success':
            return response

        patch = json.loads(self.createJSONPatch(False))

        if 'outputs' in patch:
            response['outputs'] = patch['outputs']

        if 'attributes' in patch:
            response['attributes'] = patch['attributes']

        if 'graph' in patch:
            response['graph'] = patch['graph']

        return response

    def applyEnvironment(self):
        applyEnvironment(self)

class WorkItem(AttributeOwner):
    _versionV1          = 1
    _versionV2          = 2
    _versionV2IDs       = 3
    _versionV2Command   = 4
    _versionV2LoopInfo  = 5
    _versionV2CookType  = 6
    _versionV2LoopLock  = 7
    _versionCurrent     = _versionV2LoopLock

    @classmethod
    def fromJobEnvironment(cls, item_name=None, cb_server=None, expand_strings=False, as_native=False, print_info=True):
        data_source = getWorkItemDataSource()

        if data_source == 0:
            name = os.environ.get('PDG_ITEM_NAME', item_name)
            if not name:
                pdgcmd.printlog("ERROR: PDG_ITEM_NAME must be in environment or specified via argument flag.")
                exit(1)

            work_item = cls.fromFile(getWorkItemJsonPath(name), cb_server, expand_strings, as_native)
        elif data_source == 1:
            try:
                item_id = int(os.environ['PDG_ITEM_ID'])
            except:
                pdgcmd.printlog("ERROR: PDG_ITEM_ID must be in environment")
                exit(1)

            work_item = cls.fromRPC(item_id, -1, cb_server, expand_strings, as_native)
        else:
            pdgcmd.printlog("ERROR: Unknown value PDG_ITEM_DATA_SOURCE={}".format(data_source))
            exit(1)

        if print_info:
            printHoudiniInfo(work_item)
        return work_item

    @classmethod
    def fromItemName(cls, item_name, cb_server=None, expand_strings=False, as_native=False):
        item = cls.fromFile(getWorkItemJsonPath(item_name), cb_server, expand_strings, as_native)
        return item

    @classmethod
    def fromRPC(cls, item_id, batch_index=-1, cb_server=None, expand_strings=False, as_native=False):
        json_data = pdgcmd.getWorkItemJSON(item_id, batch_index, cb_server)
        return cls.fromString(json_data, cb_server, expand_strings, as_native)

    @classmethod
    def fromFile(cls, json_path, cb_server=None, expand_strings=False, as_native=False):
        extension = os.path.splitext(json_path)
        if len(extension) > 1 and extension[-1] == '.gz':
            with gzip.open(json_path, 'rb') as f:
                json_data = f.read().decode('utf-8')
        else:
            with open(json_path, 'rb') as f:
                json_data = f.read().decode('utf-8')

        if as_native:
            import pdg
            return NativeWorkItem(
                pdg.WorkItem.loadJSONString(json_data, True), cb_server)
            
        item_data = json.loads(json_data)
        return cls(item_data, server=cb_server, expand_strings=expand_strings)

    @classmethod
    def fromString(cls, json_data, cb_server=None, expand_strings=False, as_native=False):
        if as_native:
            import pdg
            return NativeWorkItem(
                pdg.WorkItem.loadJSONString(json_data, True), cb_server)

        wi = cls(json.loads(json_data), server=cb_server, expand_strings=expand_strings)
        return wi

    @classmethod
    def fromBase64String(cls, b64_data, cb_server=None, expand_strings=False, as_native=False):
        json_data = base64.b64decode(b64_data)
        return cls.fromString(json_data, cb_server, expand_strings, as_native)

    @classmethod
    def fromServiceData(cls, job_info, cb_server=None, expand_strings=False, as_native=False):
        if 'version' in job_info:
            version = int(job_info['version'])
        else:
            version = 0

        if 'as_native' in job_info:
            as_native = int(job_info['as_native']) > 0 

        if as_native:
            import pdg
            pdg.EvaluationContext.setGlobalWorkItem(None, False)

        data = job_info['work_item']

        if version == 0:
            try:
                return cls.fromBase64String(
                    data, cb_server, expand_strings, as_native)
            except:
                return cls.fromString(
                    data, cb_server, expand_strings, as_native)
        elif version == 1:
            return cls.fromString(
                data, cb_server, expand_strings, as_native)
        else:
            return cls.fromBase64String(
                data, cb_server, expand_strings, as_native)

    def __init__(self, json_obj, batch_parent=None, batch_index=None, version=None, server=None, expand_strings=False):
        AttributeOwner.__init__(self, expand_strings)

        if not server:
            self._server = os.environ.get('PDG_RESULT_SERVER', None)
        else:
            self._server = server

        self._batchParent = batch_parent
        self._version = version
        self._tempDir = getTempDir()

        if self._batchParent:
            self._graph = self._batchParent.graph
        else:
            self._graph = Graph(expand_strings)

        if not json_obj:
            self._stub = True
            self._batchIndex = batch_index
        else:
            self._stub = False
            self._load(json_obj)

    def __hash__(self):
        return hash(self._nodeName) ^ hash(self._index) ^\
            hash(self._frame) ^ hash(self._priority) ^ self.attribHash(True)

    def attribHash(self, include_internal=True):
        if self.batchParent:
            h = self.batchParent.attribHash(include_internal)
        else:
            h = 0 

        return h ^ AttributeOwner.attribHash(self, include_internal)

    def _load(self, json_obj):
        if not self._version:
            if 'version' in json_obj:
                self._version = json_obj['version']
            else:
                self._version = self._versionV1

        if 'graph' in json_obj:
            self._graph._loadAttributes(json_obj['graph'])

        if 'workitem' in json_obj:
            item_obj = json_obj['workitem']
        else:
            item_obj = json_obj

        self._nodeName = item_obj['nodeName']
        self._schedulerName = item_obj.get('schedulerName', '')

        if self._version < self._versionV2IDs:
            self._name = item_obj['name']
        else:
            self._id = item_obj['id']
            self._name = self._nodeName + '_' + str(self._id)

        if self._version < self._versionV2Command:
            self._command = item_obj['command']
        else:
            self._command = ""

        if self._version < self._versionV2:
            self._customDataType = ""
            self._customData = ""
        else:
            self._customDataType = item_obj['customDataType']
            self._customData = item_obj['customData']

        self._batchIndex = item_obj['batchIndex']
        self._frame = item_obj['frame']
        self._frameStep = item_obj['frameStep']
        self._hasFrame = item_obj['hasFrame']
        self._index = item_obj['index']
        self._isStatic = item_obj['isStatic']
        self._priority = item_obj['priority']
        self._state = workItemState(item_obj['state'])
        self._type = workItemType(item_obj['type'])
        self._executionType = workItemExecutionType(item_obj['executionType'])

        self._loadAttributes(item_obj)

        if self._version < self._versionV2CookType:
            if item_obj['isInProcess']:
                self._cookType = workItemCookType.InProcess
            elif len(self.command):
                self._cookType = workItemCookType.OutOfProcess
            else:
                self._cookType = workItemCookType.Generate
        else:
            self._cookType = workItemCookType(item_obj['cookType'])

        if self._type == workItemType.Batch:
            self._batchCount = item_obj['batchCount']
            self._batchOffset = item_obj['batchOffset']
            self._activationMode = batchActivation(int(item_obj['activationMode']))
            self._subItems = [None]*self._batchCount

            if 'subItems' in item_obj:
                json_items = item_obj['subItems']
            else:
                json_items = []

            last = -1
            for i, sub_item_json in enumerate(item_obj['subItems']):
                self._subItems[i] = WorkItem(
                    sub_item_json,
                    batch_parent = self,
                    version = self._version,
                    expand_strings = self._expandStrings)
                last = i

            for remainder in range(last+1, self._batchCount):
                self._subItems[remainder] = WorkItem(
                    None,
                    batch_parent = self,
                    batch_index = remainder,
                    version = self._version,
                    expand_strings = self._expandStrings)

            if 'batchStart' in item_obj:
                self._batchStart = item_obj['batchStart']
            else:
                self._batchStart = 0
        else:
            self._batchCount = 0
            self._batchOffset = 0
            self._batchStart = 0
            self._activationMode = None
            self._subItems = []

    @property
    def graph(self):
        return self._graph

    @property
    def customDataType(self):
        return self._customDataType

    @property
    def customData(self):
        return self._customData

    @property
    def activationMode(self):
        return self._activationMode

    @property
    def batchSize(self):
        return self._batchCount

    @property
    def offset(self):
        return self._batchOffset

    @property
    def batchStart(self):
        return self._batchStart

    @property
    def batchIndex(self):
        return self._batchIndex

    @property
    def batchParent(self):
        return self._batchParent

    @property
    def reportName(self):
        # Use of reportName for RPC is deprecated
        if self.batchParent:
            return self.batchParent.name
        return self.name

    @property
    def reportId(self):
        if self.batchParent:
            return self.batchParent._id
        return self._id

    @property
    def command(self):
        if self._version < self._versionV2Command:
            return self._command
        else:
            return self.stringAttribValue(AttributeBase.CommandStringName) or ""

    @property
    def frame(self):
        return self._frame

    @property
    def frameStep(self):
        return self._frameStep

    @property
    def hasFrame(self):
        return self._hasFrame

    @property
    def index(self):
        return self._index

    @property
    def isInProcess(self):
        return self._cookType == workItemCookType.InProcess

    @property
    def isOutOfProcess(self):
        return self._cookType == workItemCookType.OutOfProcess

    @property
    def shouldReportChanges(self):
        return not self.isServiceMode

    @property
    def isServiceMode(self):
        return self._cookType == workItemCookType.Service

    @property
    def isBatch(self):
        return self._type == workItemType.Batch

    @property
    def isStatic(self):
        return self._isStatic

    @property
    def isCooked(self):
        return (self._state == workItemState.CookedSuccess) or \
               (self._state == workItemState.CookedCache) or \
               (self._state == workItemState.CookedFail) or \
               (self._state == workItemState.CookedCancel)

    @property
    def isSuccessful(self):
        return (self._state == workItemState.CookedSuccess) or \
               (self._state == workItemState.CookedCache)

    @property
    def isUnsuccessful(self):
        return (self._state == workItemState.CookedFail) or \
               (self._state == workItemState.CookedCancel)

    @property
    def name(self):
        return self._name

    @property
    def label(self):
        if self.hasAttribValue(AttributeBase.LabelName):
            return self.stringAttribValue(AttributeBase.LabelName)
        return self.name()

    @property
    def customState(self):
        if self.hasAttribValue(AttributeBase.CustomStateName):
            return self.stringAttribValue(AttributeBase.CustomStateName)
        return ""

    @property
    def cookPercent(self):
        if self.hasAttribValue(AttributeBase.CookPercentName):
            return self.floatAttribValue(AttributeBase.CookPercentName)
        return -1.0

    @property
    def environment(self):
        return self._values(attribFlag.EnvExport)

    @property
    def id(self):
        return self._id

    @property
    def priority(self):
        return self._priority

    @property
    def batchItems(self):
        return self._subItems

    @property
    def state(self):
        return self._state

    def setJobState(self, state):
        myid = self.reportId

        if state == workItemState.Cooking:
            pdgcmd.workItemStartCook(myid, self.batchIndex, self._server)
        elif state == workItemState.CookedSuccess:
            pdgcmd.workItemSuccess(myid, self.batchIndex, self._server)
        elif state == workItemState.CookedFail:
            if self.batchIndex > -1:
                raise TypeError("Failing batch item not supported")
            pdgcmd.workItemFailed(myid, self._server)
        else:
            raise TypeError("Job can not be set to {} state".format(state))

        self._state = state

    def appendSubItem(self):
        if not self.isBatch:
            raise TypeError("Work item is not a batch")

        item_id = pdgcmd.appendSubItem(self.reportId)
        json_str = pdgcmd.getWorkItemJSON(self.reportId, self._batchCount)
        if not json_str:
            return

        json_obj = json.loads(json_str)
        sub_item = WorkItem(
            json_obj,
            batch_parent = self,
            version = self._version,
            expand_strings = self._expandStrings)

        self._subItems.append(sub_item)
        self._batchCount += 1

        return sub_item

    def startSubItem(self, wait=False):
        if not self._batchParent:
            raise TypeError("Work item has no batch parent")

        if wait:
            pdgcmd.waitUntilReady(self.reportId, self._batchIndex)

        if self._stub:
            json_str = pdgcmd.getWorkItemJSON(self.reportId, self._batchIndex)
            if not json_str:
                return

            json_obj = json.loads(json_str)
            self._load(json_obj)

        self.setJobState(workItemState.Cooking)

    def checkSubItem(self):
        if not self._batchParent:
            raise TypeError("Work item has no batch parent")

        result = pdgcmd.checkReady(self.reportId, self._batchIndex)

        if result and self._stub:
            json_str = pdgcmd.getWorkItemJSON(self.reportId, self._batchIndex)
            if not json_str:
                return False

            json_obj = json.loads(json_str)
            self._load(json_obj)
            self._stub = False

        return result

    def cookSubItem(self, state=workItemState.CookedSuccess, duration=0):
        if not self._batchParent:
            raise TypeError("Work item has no batch parent")

        self.setJobState(state)

    def cookWarning(self, message):
        pdgcmd.warning(message, self.reportId, self._server)

    @property
    def type(self):
        return self._type

    @property
    def executionType(self):
        return self._executionType

    @property
    def cookType(self):
        return self._cookType

    @property
    def subItems(self):
        return self._subItems

    @property
    def tempDir(self):
        return self._tempDir

    def __getitem__(self, key):
        if not key in self._attributes and self.batchParent:
            return self.batchParent._attributes[key]
        return self._attributes[key]

    def addError(self, message, fail_task=False, timestamp=True):
        pdgcmd.printlog(message, prefix='ERROR', timestamp=timestamp)
        if fail_task:
            if not self.isServiceMode:
                sys.exit(-1)
            else:
                raise CookError()

    def addMessage(self, message, verbosity=pdgcmd.LogDefault, timestamp=True):
        pdgcmd.printlog(
            message,
            verbosity=verbosity,
            timestamp=timestamp)

    def addWarning(self, message, verbosity=pdgcmd.LogDefault, timestamp=True):
        pdgcmd.printlog(
            message,
            verbosity=verbosity,
            prefix='WARNING',
            timestamp=timestamp)

    def hasAttrib(self, key):
        if not AttributeOwner.hasAttrib(self, key):
            if self.batchParent:
                return self.batchParent.hasAttrib(key)
            return False
        return True

    def attrib(self, key, attrib_type=attribType.Undefined, initialized=True):
        attrib = AttributeOwner.attrib(self, key, attrib_type, initialized)
        if attrib is None:
            if self.batchParent and initialized:
                return self.batchParent.attrib(key, attrib_type)
            return None
        return attrib

    def attribNames(self, attrib_type=attribType.Undefined):
        names = set()
        if self.batchParent:
            names.update(self.batchParent.attribNames(attrib_type))
        names.update(AttributeOwner.attribNames(self, attrib_type))
        return list(names)

    def attribValues(self, attrib_type=attribType.Undefined):
        if self.batchParent:
            d = self.batchParent.attribValues(attrib_type)
        else:
            d = {}

        d.update(AttributeOwner.attribValues(self, attrib_type))
        return d

    @property
    def outputFiles(self):
        attrib = self.attrib(AttributeBase.OutputFilesName)
        if attrib:
            return attrib._queryValues(1)
        else:
            return []

    @property
    def inputFiles(self):
        return self.fileAttribArray(AttributeBase.InputFilesName)

    def inputFilesForTag(self, tag):
        if not self.inputFiles:
            return []

        input_files = []
        for input_file in self.inputFiles:
            if input_file.tag.startswith(tag) and input_file.type != 1:
                input_files.append(input_file)

        return input_files

    @property
    def inputResultData(self):
        return self.inputFiles

    def inputResultDataForTag(self, tag):
        return self.inputFilesForTag(tag)

    def addOutputFile(self, path, tag="", hash_code=0, own=False):
        new_file = File(path, tag, hash_code, own)

        self.setFileAttrib(AttributeBase.OutputFilesName, new_file, -1)
        self.setFileAttrib(AttributeBase.AddedFilesName, new_file, -1)

        if self.shouldReportChanges:
            pdgcmd.addOutputFile(
                path,
                workitem_id=self.reportId,
                subindex=self.batchIndex,
                result_data_tag=tag,
                hash_code=hash_code,
                server_addr=self._server,
                raise_exc=False)

    def addOutputFiles(self, paths, tag="", hash_codes=[], own=False):
        if not paths:
            return

        if len(paths) == 1:
            if not tag:
                tag = ""
            elif isIterable(tag):
                tag = tag[0]

            if len(hash_codes) > 0:
                self.addOutputFile(paths[0], tag, hash_codes[0], own)
            else:
                self.addOutputFile(paths[0], tag, 0, own)
            return

        for i, path in enumerate(paths):
            hash_code = hash_codes[i] if i < len(hash_codes) else 0
            if not tag:
                file_tag = ""
            elif isIterable(tag):
                file_tag = tag[i] if i < len(tag) else ""
            else:
                file_tag = tag

            new_file = File(path, file_tag, hash_code, own)

            self.setFileAttrib(AttributeBase.OutputFilesName, new_file, -1)
            self.setFileAttrib(AttributeBase.AddedFilesName, new_file, -1)

        if self.shouldReportChanges:
            pdgcmd.addOutputFiles(
                paths,
                workitem_id=self.reportId,
                subindex=self.batchIndex,
                output_file_tag=tag,
                hash_codes=hash_codes,
                server_addr=self._server,
                raise_exc=False)

    def addResultData(self, path, tag="", hash_code=0, own=False):
        self.addOutputFile(path, tag, hash_code, own)

    def setCustomState(self, custom_state):
        if self.shouldReportChanges:
            pdgcmd.workItemSetCustomState(
                custom_state,
                workitem_id=self.reportId,
                subindex=self.batchIndex)

    def setCookPercent(self, cook_percent):
        if self.shouldReportChanges:
            pdgcmd.workItemSetCookPercent(
                cook_percent,
                workitem_id=self.reportId,
                subindex=self.batchIndex)

    def invalidateCache(self):
        if self.shouldReportChanges:
            pdgcmd.invalidateCache(
                workitem_id=self.reportId,
                subindex=self.batchIndex,
                server_addr=self._server)

    def localizePath(self, path):
        return localizePath(path)

    @classmethod
    def serviceFailure(cls, msg=''):
        response = {
            'status': 'failed',
            'msg': msg,
            'version': cls._versionCurrent,
            'memory' : getMemoryUsage()
        }

        return response

    def serviceResponse(self, status, msg=''):
        if not self.isServiceMode:
            return {}

        response = {
            'status': status,
            'msg': msg,
            'version': self._versionCurrent,
            'memory' : getMemoryUsage()
        }

        if status != 'success':
            return response

        response['outputs'] = []

        attrib = self.attrib(AttributeBase.AddedFilesName)
        if attrib:
            values = attrib._queryValues(1)
            if values:
                for value in values:
                    response['outputs'].append(
                        {"file" : value.path, "tag" : value.tag})

        response['attributes'] = {}
        for attrib_name in self.attribNames():
            attrib = self[attrib_name]

            if not attrib or not attrib.changed or attrib.builtin:
                continue

            response['attributes'][attrib_name] = attrib.asJSON()

        response['graph'] = {}
        for attrib_name in self._graph.attribNames():
            attrib = self._graph[attrib_name]
            if not attrib or not attrib.changed or attrib.builtin:
                continue

            response['graph'][attrib_name] = attrib.asJSON()

        return response

    def applyEnvironment(self):
        applyEnvironment(self)

def strData(item, field, index=0):
    """
    Returns string data with the given name and index.
    Raises ValueError if not found.
    item        item to search
    field       data field name
    index       index of data in string array
    """
    if item is None:
        raise TypeError("item is None")
    val = item.stringAttribValue(field, index)
    if val is None:
        val = item.fileAttribValue(field, index)
        if val is not None:
            return val.local_path

        if not hasStrData(item, field):
            raise ValueError("String data '{}' not found on {}".format(field, item.name))
        else:
            raise IndexError("Invalid index {} for string data '{}'".format(index, field))
    return val

def strDataArray(item, field):
    """
    Returns string data array for the given name.
    Raises ValueError if not found.
    item        item to search
    field       data field name
    """
    if item is None:
        raise TypeError("item is None")
    val = item.stringAttribArray(field)
    if val is None:
        val = item.fileAttribArray(field)
        if val is not None:
            return [v.path for v in val]

        raise ValueError("String data '{}' not found on {}".format(field, item.name))
    return val

def hasStrData(item, field, index=0):
    """
    Returns True if the item has string data with the given name
    and the given index.
    item        item to search
    field       string tag for data
    index       index of data in int array
    """
    if item is None:
        raise TypeError("item is None")
    return item.stringAttribValue(field, index) is not None

def intData(item, field, index=0):
    """
    Returns int data with the given tag and index.
    Raises ValueError if not found.
    item        item to search
    field       string tag for data
    index       index of data in int array
    """
    if item is None:
        raise TypeError("item is None")
    val = item.intAttribValue(field, index)
    if val is None:
        if not hasIntData(item, field):
            raise ValueError("Int data '{}' not found on {}".format(field, item.name))
        else:
            raise IndexError("Invalid index {} for int data '{}'".format(index, field))
    return val

def intDataArray(item, field):
    """
    Returns int data array for the given tag.
    Raises ValueError if not found.
    item        item to search
    field       string tag for data
    """
    if item is None:
        raise TypeError("item is None")
    val = item.intAttribArray(field)
    if val is None:
        raise ValueError("Int data '{}' not found on {}".format(field, item.name))
    return val

def hasIntData(item, field, index=0):
    """
    Returns True if the item has int data with the given name
    and the given index.
    item        item to search
    field       string tag for data
    index       index of data in int array
    """
    if item is None:
        raise TypeError("item is None")
    return item.intAttribValue(field, index) is not None

def floatData(item, field, index=0):
    """
    Returns float data with the given tag and index.
    Raises ValueError if not found.
    item        item to search
    field       string tag for data
    index       index of data in float array
    """
    if item is None:
        raise TypeError("item is None")
    val = item.floatAttribValue(field, index)
    if val is None:
        if not hasFloatData(item, field):
            raise ValueError("Float data '{}' not found on {}".format(field, item.name))
        else:
            raise IndexError("Invalid index {} for float data '{}'".format(index, field))
    return val

def floatDataArray(item, field):
    """
    Returns float data array for the given tag.
    Raises ValueError if not found.
    item        item to search
    field       string tag for data
    """
    if item is None:
        raise TypeError("item is None")
    val = item.floatAttribArray(field)
    if val is None:
        raise ValueError("Float data '{}' not found on {}".format(field, item.name))
    return val

def hasFloatData(item, field, index=0):
    """
    Returns True if the item has float data with the given name
    and the given index.
    item        item to search
    field       string tag for data
    index       index of data in int array
    """
    if item is None:
        raise TypeError("item is None")
    return item.floatAttribValue(field, index) is not None

def applyEnvironment(work_item):
    """
    Applies environment variables to the scene, or the process environment if
    the hou module is not available.
    """

    try:
        import hou
        for k, v in list(work_item.environment.items()):
            hou.hscript('setenv {}={}'.format(k, v))
        hou.hscript('varchange')
    except:
        import os
        for k, v in list(work_item.environment.items()):
            os.environ[k] = str(v)

def applyWedges(work_item, verbose=True):
    """
    Applies wedges to the scene, based on attributes on the specified
    work item
    """
    try:
        import hou
    except:
        work_item.addError(
            "Unable to apply wedges in non-Houdini Python session")
        return

    if not work_item:
        raise TypeError("work_item is None")

    def _applyParm(parm, index, name, value, value_type, channel):
        if value_type == 0:
            expr = "@{}.{}".format(name, index)
            is_expr = True
        elif value_type == 1:
            value = value
            is_expr = False
        else:
            expr = str(value)
            is_expr = True

        if is_expr:
            try:
                if parm.expression() == expr:
                    if verbose:
                        work_item.addMessage(
                            'Parm "{}" at index "{}" was unchanged'
                            ' for the current subitem'.format(channel, index))
                    return False
            except:
                pass

        parm.deleteAllKeyframes()

        if is_expr:
            if verbose:
                work_item.addMessage(
                    'Setting parm "{}" at index "{}" to expr "{}"'.format(
                    channel, index, expr))
            parm.setExpression(expr, hou.exprLanguage.Hscript)
        else:
            if verbose:
                work_item.addMessage(
                    'Setting parm "{}" at index "{}" to value "{}"'.format(
                    channel, index, value))
            parm.set(value)

        return True

    # Check for wedge attributes
    attribs = work_item.stringAttribArray("wedgeattribs")
    if not attribs:
        return

    # For each attrib, try to find a channel array and value array
    for attrib_name in attribs:
        value_attrib = work_item.attrib(attrib_name)
        channel_attrib = work_item.attrib("{}channel".format(attrib_name))
        if not channel_attrib or not value_attrib:
            continue

        channel = channel_attrib.value()
        value = value_attrib.value()

        if verbose:
            work_item.addMessage(
                'Setting channels for wedge attrib "{}"'.format(
                attrib_name))

        value_type_attrib = work_item.attrib("{}valuetype".format(attrib_name))
        if value_type_attrib:
            value_type = value_type_attrib.value()
        else:
            value_type = 0

        if verbose:
            if value_type == 0:
                value_type_str = "Attribute Reference"
            elif value_type == 1:
                value_type_str = "Parameter Value"
            elif value_type == 2:
                value_type_str = "Parameter Expression"
            work_item.addMessage(
                'Setting value for "{}" with type "{}"'.format(
                attrib_name, value_type_str))

        # Apply values to channels
        parm = hou.parm(channel)
        if parm:
            _applyParm(parm, 0, attrib_name, value, value_type, channel)

            callback_attrib = work_item.intAttribValue(
                "{}callback".format(attrib_name), 0)
            if callback_attrib:
                parm.pressButton()
        else:
            parm_tuple = hou.parmTuple(channel)
            if parm_tuple:
                idx = 0
                for parm in parm_tuple:
                    _applyParm(
                        parm, idx, attrib_name, value, value_type, channel)
                    idx += 1
            elif verbose:
                work_item.addWarning(
                    'Wedge parm "{}" not found'.format(channel))

def printHoudiniInfo(work_item=None):
    try:
        import hou

        msg = "Running Houdini {} with PID {}".format(
            hou.applicationVersionString(), os.getpid())

        if work_item:
            work_item.addMessage(msg);
        else:
            pdgcmd.printlog(msg);

        for var in ["HFS", "HOUDINI_TEMP_DIR", "HOUDINI_PATH"]:
            msg = "{} = '{}'".format(var, hou.getenv(var))
            if work_item:
                work_item.addMessage(msg, verbosity=pdgcmd.LogVerbose)
            else:
                pdgcmd.printlog(msg, verbosity=pdgcmd.LogVerbose)
    except ImportError:
        msg = "Running '{}' with PID {}".format(sys.executable, os.getpid())

        if work_item:
            work_item.addMessage(msg)
        else:
            pdgcmd.printlog(msg)
