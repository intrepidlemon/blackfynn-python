# -*- coding: utf-8 -*-

import os
import re
from uuid import uuid4
from blackfynn import settings
from blackfynn.utils import (
    infer_epoch, get_data_type, value_as_type, usecs_to_datetime
)
from dateutil import parser
import datetime
import requests
import numpy as np
import pandas as pd
import dateutil.parser

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# Helpers
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

def get_package_class(data):
    """
    Determines package type and returns appropriate class.
    """
    content = data.get('content', data)
    if 'packageType' not in content:
        p = Dataset
    else:
        ptype = content['packageType'].lower()
        if ptype == 'collection':
            p = Collection
        elif ptype == 'timeseries':
            p = TimeSeries
        elif ptype == 'tabular':
            p = Tabular
        elif ptype == 'dataset':
            p = Dataset
        else:
            p = DataPackage

    return p

def _update_self(self, updated):
    if self.id != updated.id:
        raise Exception("cannot update {} with {}".format(self, updated))

    self.__dict__.update(updated.__dict__)

    return self

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# Basics
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

class Property(object):
    """
    Property of a blackfynn object. 

    Args:
        key (str): the key of the property
        value (str,number): the value of the property

        fixed (bool): if true, the value cannot be changed after the property is created
        hidden (bool): if true, the value is hidden on the platform
        category (str): the category of the property, default: "Blackfynn"
        data_type (str): one of 'string', 'integer', 'double', 'date', 'user'

    """
    _data_types = ['string', 'integer', 'double', 'date', 'user', 'boolean']
    def __init__(self, key, value, fixed=False, hidden=False, category="Blackfynn", data_type=None):
        self.key = key
        self.fixed = fixed
        self.hidden = hidden
        self.category = category

        if data_type is None or (data_type.lower() not in self._data_types):
            dt,v = get_data_type(value)
            self.data_type = dt
            self.value = v
        else:
            self.data_type = data_type
            self.value = value_as_type(value, data_type.lower())

    def as_dict(self):
        """
        Representation of instance as dictionary, used when calling API.
        """
        return {
            "key": self.key,
            "value": str(self.value), # value needs to be string :-(
            "dataType": self.data_type,
            "fixed": self.fixed,
            "hidden": self.hidden,
            "category": self.category
        }

    @classmethod
    def from_dict(cls, data, category='Blackfynn'):
        """
        Create an instance from dictionary, used when handling API response.
        """
        return cls(
            key=data['key'],
            value=data['value'],
            category=category,
            fixed=data['fixed'],
            hidden=data['hidden'],
            data_type=data['dataType']
        )

    def __str__(self):
        return self.__repr__()

    def __repr__(self):
        return u"<Property key='{}' value='{}' type='{}' category='{}'>" \
                    .format(self.key, self.value, self.data_type, self.category)


def _get_all_class_args(cls):
    # possible class arguments
    if cls == object:
        return set()
    class_args = set()
    for base in cls.__bases__:
        # get all base class argument variables
        class_args.update(_get_all_class_args(base))
    # return this class and all base-class variables
    class_args.update(cls.__init__.__func__.func_code.co_varnames)
    return class_args


class BaseNode(object):
    """
    Base class to serve all objects
    """
    _api = None
    _object_key = 'content'

    def __init__(self, id=None):
        self.id = id


    @classmethod
    def from_dict(cls, data, api=None, object_key=None):
        # which object_key are we going to use?
        if object_key is not None:
            obj_key = object_key
        else:
            obj_key = cls._object_key

        # validate obj_key
        if obj_key == '' or obj_key is None:
            content = data
        else:
            content = data[obj_key]

        class_args = _get_all_class_args(cls)

        # find overlapping keys
        kwargs = {}
        thing_id = content.pop('id', None)
        for k,v in content.iteritems():
            # check lower case var names
            k_lower = k.lower()
            # check camelCase --> camel_case
            k_camel = re.sub(r'[A-Z]', lambda x: '_'+x.group(0).lower(), k)
            # check s3case --> s3_case
            k_camel_num = re.sub(r'[0-9]', lambda x: x.group(0)+'_', k)

            # match with existing args
            if k_lower in class_args:
                key = k_lower
            elif k_camel in class_args:
                key = k_camel
            elif k_camel_num in class_args:
                key = k_camel_num
            else:
               key = k

            # assign
            kwargs[key] = v

        # init class with args
        item = cls.__new__(cls)
        cls.__init__(item, **kwargs)

        if thing_id is not None:
            item.id = thing_id

        if api is not None:
            item._api = api
            item._api.core.set_local(item)

        return item

    def __eq__(self, item):
        if self.exists and item.exists:
            return self.id == item.id
        else:
            return self is item

    @property
    def exists(self):
        """ 
        Whether or not the instance of this object exists on the platform.
        """
        return self.id is not None

    def _check_exists(self):
        if not self.exists:
            raise Exception('Object must be created on the platform before method is called.') 

    def __str__(self):
        return self.__repr__()


class BaseDataNode(BaseNode):
    """
    Base class to serve all "data" node-types on platform, e.g. Packages and Collections.
    """
    _type_name = 'packageType'

    def __init__(self, name, type,
            parent=None,
            owner_id=None,
            dataset_id=None,
            id=None,
            provenance_id=None, **kwargs):

        super(BaseDataNode, self).__init__(id=id)

        self.name = name
        self._properties = {}
        if isinstance(parent, basestring) or parent is None:
            self.parent = parent
        elif isinstance(parent, Collection):
            self.parent = parent.id
        else:
            raise Exception("Invalid parent {}".format(parent))
        self.type = type
        self.dataset = dataset_id
        self.owner_id = owner_id
        self.provenance_id = provenance_id

        self.state = kwargs.pop('state', None)
        self.created_at = kwargs.pop('createdAt', None)
        self.updated_at = kwargs.pop('updatedAt', None)

    def update_properties(self):
        self._api.data.update_properties(self)

    def _set_properties(self, *entries):
        # Note: Property is stored as dict of key:properties-entry to enable
        #       over-write of properties values based on key
        for entry in entries:
            assert type(entry) is Property, "Properties wrong type"
            if entry.category not in self._properties:
                self._properties[entry.category] = {}
            self._properties[entry.category].update({entry.key:entry})

    def add_properties(self, *entries):
        """ 
        Add properties to object.

        Args:
            entries (list): list of Property objects to add to this object

        """
        self._set_properties(*entries)

        # update on platform
        if self.exists:
            self.update_properties()

    def insert_property(self, key, value, fixed=False, hidden=False, category="Blackfynn", data_type=None):
        """
        Add property to object using simplified interface.

        Args:
            key (str): the key of the property
            value (str,number): the value of the property

            fixed (bool): if true, the value cannot be changed after the property is created
            hidden (bool): if true, the value is hidden on the platform
            category (str): the category of the property, default: "Blackfynn"
            data_type (str): one of 'string', 'integer', 'double', 'date', 'user'

        Note:
            This method is being depreciated in favor of ``set_property()`` method (see below).

        """
        return self.set_property(
            key=key,
            value=value,
            fixed=fixed,
            hidden=hidden,
            category=category,
            data_type=data_type
        )

    def set_property(self, key, value, fixed=False, hidden=False, category="Blackfynn", data_type=None):
        """
        Add property to object using simplified interface.

        Args:
            key (str): the key of the property
            value (str,number): the value of the property

            fixed (bool): if true, the value cannot be changed after the property is created
            hidden (bool): if true, the value is hidden on the platform
            category (str): the category of the property, default: "Blackfynn"
            data_type (str): one of 'string', 'integer', 'double', 'date', 'user'

        """
        self._set_properties(
            Property(
                key=key,
                value=value,
                fixed=fixed,
                hidden=hidden,
                category=category,
                data_type=data_type)
        )
        # update on platform, if possible
        if self.exists:
            self.update_properties()

    @property
    def properties(self):
        """
        Returns a list of properties attached to object.
        """
        props = []
        for category in self._properties.values():
            props.extend(category.values())
        return props

    def get_property(self, key, category='Blackfynn'):
        """
        Returns a single property for the provided key, if available

        Args:
            key (str): key of the desired property
            category (str, optional): category of property

        Returns:
            object of type ``Property``

        Example::

            pkg.set_property('quality', 85.0)
            pkg.get_property('quality')

        """
        return self._properties[category].get(key, None)

    def remove_property(self, key, category='Blackfynn'):
        """
        Removes property of key ``key`` and category ``category`` from the object.

        Args:
            key (str): key of property to remove
            category (str, optional): category of property to remove

        """
        if key in self._properties[category]:
            # remove by setting blank
            self._properties[category][key].value = ""
            # update remotely
            self.update_properties()
            # get rid of it locally
            self._properties[category].pop(key)

    def update(self, **kwargs):
        """
        Updates object on the platform (with any local changes) and syncs
        local instance with API response object.

        Exmple::

            pkg = bf.get('N:package:1234-1234-1234-1234')
            pkg.name = "New name"
            pkg.update()

        """
        self._check_exists()
        r = self._api.core.update(self, **kwargs)
        _update_self(self, r)

    def delete(self):
        """
        Delete object from platform.
        """
        self._check_exists()
        r = self._api.core.delete(self)
        self.id = None

    def set_ready(self, **kwargs):
        """
        Set's the package's state to ``READY``
        """
        self.state = "READY"
        return self.update(**kwargs)

    def set_unavailable(self):
        """
        Set's the package's state to ``UNAVAILABLE``
        """
        self._check_exists()
        self.state = "UNAVAILABLE"
        return self.update()

    def set_error(self):
        """
        Set's the package's state to ``ERROR``
        """
        self._check_exists()
        self.state = "ERROR"
        return self.update()

    def as_dict(self):
        d = {
            "name": self.name,
            self._type_name: self.type,
            "properties": [
                m.as_dict() for m in self.properties
            ]
        }

        for k in ['parent', 'state', 'dataset']:
            kval = self.__dict__.get(k, None)
            if hasattr(self, k) and kval is not None:
                d[k] = kval
                
        if self.provenance_id is not None:
            d["provenanceId"] = self.provenance_id

        return d

    @classmethod
    def from_dict(cls, data, *args, **kwargs):
        item = super(BaseDataNode,cls).from_dict(data, *args, **kwargs)

        try:
            item.state = data['content']['state']
        except:
            pass

        # parse, store parent (ID only)
        if 'parent' in data:
            parent = data['parent']
            if isinstance(parent, basestring):
                item.parent = parent
            else:
                pkg_cls = get_package_class(parent) 
                p = pkg_cls.from_dict(parent, *args, **kwargs)
                item.parent = p.id

        def cls_add_property(prop):
            cat = prop.category
            if cat not in item._properties:
                item._properties[cat] = {}
            item._properties[cat].update({prop.key: prop})

        # parse properties
        if 'properties' in data:
            for entry in data['properties']:
                if 'properties' not in entry:
                    # flat list of properties: [entry]
                    prop = Property.from_dict(entry, category=entry['category'])
                    cls_add_property(prop)
                else:
                    # nested properties list [ {category,entry} ]
                    category = entry['category']
                    for prop_entry in entry['properties']:
                        prop = Property.from_dict(prop_entry, category=category)
                        cls_add_property(prop)

        return item


class BaseCollection(BaseDataNode):
    """
    Base class used for both ``Dataset`` and ``Collection``.
    """
    def __init__(self, name, package_type, **kwargs):
        self.storage = kwargs.pop('storage', None)
        super(BaseCollection, self).__init__(name, package_type, **kwargs)

        # items is None until an API response provides the item objects 
        # to be parsed, which then updates this instance.
        self._items = None

    def add(self, *items):
        """
        Add items to the Collection/Dataset.
        """
        self._check_exists()
        for item in items:
            # initialize if need be
            if self._items is None:
                self._items = [] 
            if isinstance(self, Dataset):
                item.parent = None
                item.dataset = self.id
            elif hasattr(self, 'dataset'):
                item.parent = self.id
                item.dataset = self.dataset

            # create, if not already created
            new_item = self._api.core.create(item)
            item.__dict__.update(new_item.__dict__)

            # add item
            self._items.append(item)

    def remove(self, *items):
        """
        Removes items, where items can be an object or the object's ID (string).
        """
        self._check_exists()
        for item in items:
            if item not in self._items:
                raise Exception('Cannot remove item, not in collection:{}'.format(item))

        self._api.data.delete(*items)
        # force refresh
        self._items = None

    @property
    def items(self):
        """
        Get all items inside Dataset/Collection (i.e. non-nested items). 

        Note:
            You can also iterate over items inside a Dataset/Colleciton without using ``.items``::

                for item in my_dataset:
                    print "item name = ", item.name

        """
        self._check_exists()
        if self._items is None:
            new_self = self._get_method(self)
            new_items = new_self._items
            self._items = new_items if new_items is not None else []

        return self._items

    @property
    def _get_method(self):
        pass

    def print_tree(self, indent=0):
        """
        Prints a tree of **all** items inside object.
        """
        self._check_exists()
        print u'{}{}'.format(' '*indent, self)
        for item in self.items:
            if isinstance(item, BaseCollection):
                item.print_tree(indent=indent+2)
            else:
                print u'{}{}'.format(' '*(indent+2), item)

    def get_items_by_name(self, name):
        """
        Get an item inside of object by name (if match is found).

        Args:
            name (str): the name of the item

        Returns:
            list of matches

        Note:
            This only works for **first-level** items, meaning it must exist directly inside the current object;
            nested items will not be returned.

        """
        self._check_exists()
        # note: non-hierarchical
        return filter(lambda x: x.name==name, self.items)

    def get_items_names(self):
        self._check_exists()
        return map(lambda x: x.name, self.items)

    def upload(self, *files, **kwargs):
        """
        Upload files into current object.

        Args:
            files: list of local files to upload.

        Example::

            my_collection.upload('/path/to/file1.nii.gz', '/path/to/file2.pdf')

        """
        self._check_exists()
        return self._api.io.upload_files(self, files, append=False, **kwargs)

    def create_collection(self, name):
        """
        Create a new collection within the current object. Collections can be created within 
        datasets and within other collections.

        Args:
            name (str): The name of the to-be-created collection

        Returns:
            The created ``Collection`` object.

        Example::

              from blackfynn import Blackfynn()
              
              bf = Blackfynn()
              ds = bf.get_dataset('my_dataset')
              
              # create collection in dataset
              col1 = ds.create_collection('my_collection')

              # create collection in collection
              col2 = col1.create_collection('another_collection')
        
        """
        c = Collection(name)
        self.add(c)
        return c

    # sequence-like method
    def __getitem__(self, i):
        self._check_exists()
        return self.items[i]

    # sequence-like method
    def __len__(self):
        self._check_exists()
        return len(self.items)

    # sequence-like method
    def __delitem__(self, key):
        self._check_exists()
        self.remove(key)

    def __iter__(self):
        self._check_exists()
        for item in self.items:
            yield item

    # sequence-like method
    def __contains__(self, item):
        """
        Tests if item is in the collection, where item can be either
        an object's ID (string) or an object's instance.
        """
        self._check_exists()
        if isinstance(item, basestring):
            some_id = self._api.data._get_id(item)
            item_ids = [x.id for x in self.items]
            contains = some_id in item_ids
        elif self._items is None:
            return False
        else:
            return item in self._items

        return contains

    def as_dict(self):
        d = super(BaseCollection, self).as_dict()
        if self.owner_id is not None:
            d['owner'] = self.owner_id
        return d

    @classmethod
    def from_dict(cls, data, *args, **kwargs):
        item = super(BaseCollection, cls).from_dict(data, *args, **kwargs)
        children = []
        if 'children' in data:
            for child in data['children']:
                pkg_cls = get_package_class(child)
                kwargs['api'] = item._api
                pkg = pkg_cls.from_dict(child, *args, **kwargs)
                children.append(pkg)
        item.add(*children)

        return item

    def __repr__(self):
        return u"<BaseCollection name='{}' id='{}'>".format(self.name, self.id)


class DataPackage(BaseDataNode):
    """
    DataPackage is the core data object representation on the platform.

    Args:
        name (str):          The name of the data package
        package_type (str):  The package type, e.g. 'TimeSeries', 'MRI', etc.

    Note:
        ``package_type`` must be a supported package type. See our data type
        registry for supported values.

    """

    def __init__(self, name, package_type, **kwargs):
        self.storage = kwargs.pop('storage', None)
        super(DataPackage, self).__init__(name=name, type=package_type, **kwargs)
        # local-only attribute
        self.session = None

    def set_view(self, *files):
        """
        Set the object(s) used to view the package, if not the file(s) or source(s).
        """
        self._check_exists()
        ids = self._api.packages.set_view(self, *files)
        # update IDs of file objects
        for i,f in enumerate(files):
            f.id = ids[i]

    def set_files(self, *files):
        """
        Sets the files of a DataPackage. Files are typically modified 
        source files (e.g. converted to a different format).
        """
        self._check_exists()
        ids = self._api.packages.set_files(self, *files)
        # update IDs of file objects
        for i,f in enumerate(files):
            f.id = ids[i]

    def set_sources(self, *files):
        """
        Sets the sources of a DataPackage. Sources are the raw, unmodified
        files (if they exist) that contains the package's data.
        """
        self._check_exists()
        ids = self._api.packages.set_sources(self, *files)
        # update IDs of file objects
        for i,f in enumerate(files):
            f.id = ids[i]

    def append_to_files(self, *files):
        """
        Append to file list of a DataPackage
        """
        self._check_exists()
        files = self._api.packages.set_files(self, *files, append=True)

    def append_to_sources(self, *files):
        """
        Appends to source list of a DataPackage.
        """
        self._check_exists()
        files = self._api.packages.set_sources(self, *files, append=True)

    @property
    def sources(self):
        """
        Returns the sources of a DataPackage. Sources are the raw, unmodified
        files (if they exist) that contains the package's data.
        """
        self._check_exists()
        return self._api.packages.get_sources(self)

    @property
    def files(self):
        """
        Returns the files of a DataPackage. Files are the possibly modified 
        source files (e.g. converted to a different format), but they could also
        be the source files themselves.
        """
        self._check_exists()
        return self._api.packages.get_files(self)

    @property
    def view(self):
        """
        Returns the object(s) used to view the package. This is typically a set of
        file objects, that may be the DataPackage's sources or files, but could also be
        a unique object specific for the viewer.
        """
        self._check_exists()
        return self._api.packages.get_view(self)

    def as_dict(self):
        d = super(DataPackage, self).as_dict()
        if self.owner_id is not None:
            d['owner'] = self.owner_id
        return d

    @classmethod
    def from_dict(cls, data, *args, **kwargs):
        item = super(DataPackage, cls).from_dict(data, *args, **kwargs)

        # parse objects
        if 'objects' in data:
            for otype in ['sources','files','view']:
                if otype not in data['objects']:
                    continue
                odata = data['objects'][otype]
                item.__dict__[otype] = [File.from_dict(x) for x in odata]

        return item

    @classmethod
    def from_id(cls, id):
        return self._api.packages.get(id)

    def __repr__(self):
        return u"<DataPackage name='{}' id='{}'>".format(self.name, self.id)


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# Files
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

class File(BaseDataNode):
    """
    File node on the Blackfynn platform. Points to some S3 location.

    Args:
        name (str):      Name of the file (without extension)
        s3_key (str):    S3 key of file
        s3_bucket (str): S3 bucket of file
        file_type (str): Type of file, e.g. 'MPEG', 'PDF'
        size (long): Size of file

    Note:
        ``file_type`` must be a supported file type. See our file type registry
        for a list of supported file types.


    """
    _type_name = 'fileType'

    def __init__(self, name, s3_key, s3_bucket, file_type, size, pkg_id=None, **kwargs):
        super(File, self).__init__(name, type=file_type, **kwargs)

        # data
        self.s3_key = s3_key
        self.s3_bucket = s3_bucket
        self.size = size
        self.pkg_id = pkg_id
        self.local_path = None

    def as_dict(self):
        d = super(File, self).as_dict()
        d.update({
            "s3bucket": self.s3_bucket,
            "s3key": self.s3_key,
            "size": self.size
        })
        d.pop('parent', None)
        props = d.pop('properties')
        return {
            'objectType': 'file',
            'content': d,
            'properties': props
        }

    @property
    def url(self):
        """
        The presigned-URL of the file.
        """
        self._check_exists()
        return self._api.packages.get_presigned_url_for_file(self.pkg_id, self.id)

    def download(self, destination):
        """
        Download the file.

        Args:
            destination (str): path for downloading; can be absolute file path, 
                               prefix or destination directory.

        """
        if self.type=="DirectoryViewerData":
            raise NotImplementedError("Downloading S3 directories is currently not supported")

        if os.path.isdir(destination):
            # destination dir
            f_local = os.path.join(destination, os.path.basename(self.s3_key))
        if '.' not in os.path.basename(destination):
            # destination dir + prefix
            f_local = destination + '_' + os.path.basename(self.s3_key)
        else:
            # exact location
            f_local = destination

        r = requests.get(self.url, stream=True)
        with open(f_local, 'wb') as f:
            for chunk in r.iter_content(chunk_size=1024):
                if chunk: f.write(chunk)

        # set local path
        self.local_path = f_local

        return f_local

    def __repr__(self):
        return u"<File name='{}' type='{}' key='{}' bucket='{}' size='{}' id='{}'>" \
                    .format(self.name, self.type, self.s3_key, self.s3_bucket, self.size, self.id)


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# Time series
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

class TimeSeries(DataPackage):
    """
    Represents a timeseries package on the platform. TimeSeries packages
    contain channels, which contain time-dependent data sampled at some
    frequency.

    Args:
        name:  The name of the timeseries package
        
    """
    def __init__(self, name, **kwargs):
        kwargs.pop('package_type', None)
        super(TimeSeries,self).__init__(name=name, package_type="TimeSeries", **kwargs)


    def streaming_credentials(self):
        self._check_exists()
        return self._api.timeseries.get_streaming_credentials(self)

    @property
    def start(self):
        """
        The start time of time series data (over all channels)
        """
        self._check_exists()
        return sorted([x.start for x in self.channels])[0]

    @property
    def end(self):
        """
        The end time (in usecs) of time series data (over all channels)
        """
        self._check_exists()
        return sorted([x.end for x in self.channels])[-1]

    def limits(self):
        """
        Returns time limit tuple (start, end) of package.
        """
        channels = self.channels
        start = sorted([x.start for x in channels])[0]
        end   = sorted([x.end   for x in channels])[-1]
        return start, end

    @property
    def channels(self):
        """
        Returns list of Channel objects associated with package.

        Note:
            This is a dynamically generated property, so every call will make an API request.

            Suggested usage::

                channels = ts.channels
                for ch in channels:
                    print ch
            
            This will be much slower, as the API request is being made each time.::

                for ch in ts.channels:
                    print ch

        """
        self._check_exists()
        # always dynamically return channel list
        return self._api.timeseries.get_channels(self)

    def get_channel(self, channel):
        """
        Get channel by ID.

        Args:
            channel (str): ID of channel
        """
        self._check_exists()
        return self._api.timeseries.get_channel(self, channel)

    def add_channels(self, *channels):
        """
        Add channels to TimeSeries package.

        Args:
            channels: list of Channel objects.

        """
        self._check_exists()
        for channel in channels:
            ch = self._api.timeseries.create_channel(self, channel)
            channel.__dict__.update(ch.__dict__)

    def remove_channels(self, *channels):
        """
        Remove channels from TimeSeries package.

        Args:
            channels: list of Channel objects or IDs
        """
        self._check_exists()
        for channel in channels:
            self._api.timeseries.delete_channel(channel)
            channel.id = None
            channel._pkg = None

    # ~~~~~~~~~~~~~~~~~~
    # Data 
    # ~~~~~~~~~~~~~~~~~~
    def get_data(self, start=None, end=None, length=None, channels=None, use_cache=settings.use_cache):
        """
        Get timeseries data between ``start`` and ``end`` or ``start`` and ``start + length`` 
        on specified channels (default all channels).

        Args:
            start (optional): start time of data (usecs or datetime object)
            end (optional): end time of data (usecs or datetime object)
            length (optional): length of data to retrieve, e.g. '1s', '5s', '10m', '1h'
            channels (optional): list of channel objects or IDs, default all channels.

        Note:
            Data requests will be automatically chunked and combined into a single Pandas
            DataFrame. However, you must be sure you request only a span of data that
            will properly fit in memory. 

            See ``get_data_iter`` for an iterator approach to timeseries data retrieval.

        Example:

            Get 5 seconds of data from start over all channels::

                data = ts.get_data(length='5s')

            Get data betwen 12345 and 56789 (representing usecs since Epoch)::

                data = ts.get_data(start=12345, end=56789)

            Get first 10 seconds for the first two channels::

                data = ts.get_data(length='10s', channels=ts.channels[:2])

        """
        self._check_exists()
        return self._api.timeseries.get_ts_data(self,start=start, end=end, length=length, channels=channels, use_cache=use_cache)

    def get_data_iter(self, channels=None, start=None, end=None, length=None, chunk_size=None, use_cache=settings.use_cache):
        """
        Returns iterator over the data. Must specify **either ``end`` OR ``length``**, not both.

        Args:
            channels (optional): channels to retrieve data for (default: all)
            start: start time of data (default: earliest time available).
            end: end time of data (default: latest time avialable).
            length: some time length, e.g. '1s', '5m', '1h' or number of usecs
            chunk: some time length, e.g. '1s', '5m', '1h' or number of usecs

        Returns:
            iterator of Pandas Series, each the size of ``chunk_size``.

        """
        self._check_exists()
        return self._api.timeseries.get_ts_data_iter(self, channels=channels, start=start, end=end, length=length, chunk_size=chunk_size, use_cache = use_cache)

    def write_annotation_file(self,file,layer_names = None):
        """
        Writes all layers to a csv .bfannot file

        Args:
            file : path to .bfannot output file. Appends extension if necessary
            layer_names (optional): List of layer names to write

        """

        return self._api.timeseries.write_annotation_file(self,file,layer_names)

    def append_annotation_file(self,file):
        """
        Processes .bfannot file and adds to timeseries package.

        Args:
            file : path to .bfannot file

        """
        self._check_exists()
        return self._api.timeseries.process_annotation_file(self,file)

    def append_files(self, *files, **kwargs):
        self._check_exists()
        return self._api.io.upload_files(self, files, append=True, **kwargs)

    def stream_data(self, data):
        self._check_exists()
        return self._api.timeseries.stream_data(self, data)

    # ~~~~~~~~~~~~~~~~~~
    # Annotations
    # ~~~~~~~~~~~~~~~~~~

    @property
    def layers(self):
        """
        List of annotation layers attached to TimeSeries package.
        """
        self._check_exists()
        # always dynamically return annotation layers
        return self._api.timeseries.get_annotation_layers(self)

    def get_layer(self, id_or_name):
        """
        Get annotation layer by ID or name.

        Args:
            id_or_name: layer ID or name
        """
        self._check_exists()
        layers = self.layers
        matches = filter(lambda x: x.id==id_or_name, layers)
        if len(matches) == 0:
            matches = filter(lambda x: x.name==id_or_name, layers)

        if len(matches) == 0:
            raise Exception("No layers match criteria.")
        if len(matches) > 1:
            raise Exception("More than one layer matched criteria")
            
        return matches[0]

    def add_layer(self,layer,description=None):
        """
        Args:
            layer:   TimeSeriesAnnotationLayer object or name of annotation layer
            description (str, optional):   description of layer

        """
        self._check_exists()
        return self._api.timeseries.create_annotation_layer(self,layer=layer,description=description)

    def add_annotations(self,layer,annotations):
        """
        Args:
            layer: either TimeSeriesAnnotationLayer object or name of annotation layer.
                   Note that non existing layers will be created.
            annotations: TimeSeriesAnnotation object(s)

        Returns:
            list of TimeSeriesAnnotation objects
        """
        self._check_exists()
        cur_layer = self._api.timeseries.create_annotation_layer(self,layer=layer,description=None)
        return self._api.timeseries.create_annotations(layer=cur_layer, annotations=annotations)

    def insert_annotation(self,layer,annotation,start=None,end=None,channel_ids=None,annotation_description=None):
        """
        Insert annotations using a more direct interface, without the need for layer/annotation objects.

        Args:
            layer: str of new/existing layer or annotation layer object
            annotation: str of annotation event

            start (optional): start of annotation
            end (optional): end of annotation
            channels_ids (optional): list of channel IDs to apply annotation
            annotation_description (optional): description of annotation 

        Example:
            To add annotation on layer "my-events" across all channels::

                ts.insert_annotation('my-events', 'my annotation event')

            To add annotation to first channel::

                ts.insert_annotation('my-events', 'first channel event', channel_ids=ts.channels[0])

        """
        self._check_exists()
        cur_layer = self._api.timeseries.create_annotation_layer(self,layer=layer,description=None)
        return self._api.timeseries.create_annotation(
                layer=cur_layer,
                annotation=annotation,
                start=start,
                end=end,
                channel_ids=channel_ids,
                description=annotation_description)

    def delete_layer(self, layer):
        """
        Delete annotation layer.

        Args:
            layer: annotation layer object

        """
        self._check_exists()
        return self._api.timeseries.delete_annotation_layer(layer)

    def query_annotation_counts(self, channels, start, end, layer=None):
        """
        Get annotation counts between ``start`` and ``end``.

        Args:
            channels: list of channels to query over
            start: start time of query (datetime object or usecs from Epoch)
            end: end time of query (datetime object or usecs from Epoch)

        """
        self._check_exists()
        return self._api.timeseries.query_annotation_counts(
            channels=channels,start=start,end=end,layer=layer)

    def __repr__(self):
        return u"<TimeSeries name=\'{}\' id=\'{}\'>".format(self.name, self.id)


class TimeSeriesChannel(BaseDataNode):
    """
    TimeSeriesChannel represents a single source of time series data. (e.g. electrode)

    Args:
        name (str):                   Name of channel
        rate (float):                 Rate of the channel (Hz)
        start (optional):             Absolute start time of all data (datetime obj)
        end (optional):               Absolute end time of all data (datetime obj)
        unit (str, optional):         Unit of measurement
        channel_type (str, optional): One of 'continuous' or 'event'
        source_type (str, optional):  The source of data, e.g. "EEG"
        group (str, optional):        The channel group, default: "default"

    """
    def __init__(self, name, rate, start=0, end=0, unit='V', channel_type='continuous', source_type='unspecified', group="default", last_annot=0, spike_duration=None, **kwargs):
        self.channel_type = channel_type.upper()

        super(TimeSeriesChannel, self).__init__(name=name, type=self.channel_type,**kwargs)

        self.rate = rate
        self.unit = unit
        self.last_annot = last_annot
        self.group = group
        self.start = start
        self.end = end
        self.spike_duration = spike_duration

        self.set_property("Source Type", source_type.upper(), fixed=True, hidden=True, category="Blackfynn")

        ###  local-only
        # parent package
        self._pkg = None
        # sample period (in usecs)
        self._sample_period = 1.0e6/self.rate

    @property
    def start(self):
        """
        The start time of channel data (microseconds since Epoch)
        """
        return self._start

    @start.setter
    def start(self, start):
        self._start = infer_epoch(start)

    @property
    def start_datetime(self):
        return usecs_to_datetime(self._start)

    @property
    def end(self):
        """
        The end time (in usecs) of channel data (microseconds since Epoch)
        """
        return self._end

    @end.setter
    def end(self, end):
        self._end = infer_epoch(end)

    @property
    def end_datetime(self):
        return usecs_to_datetime(self._end)

    def _page_delta(self, page_size):
        return long((1.0e6/self.rate) * page_size)

    def update(self):
        self._check_exists()
        r = self._api.timeseries.update_channel(self)
        self.__dict__.update(r.__dict__)

    @property
    def segments(self):
        # TODO: query API to get segments
        raise NotImplementedError

    @property
    def gaps(self):
        # TODO: infer gaps from segments
        raise NotImplementedError

    def update_properties(self):
        self._api.timeseries.update_channel_properties(self)

    def get_data(self, start=None, end=None, length=None, use_cache=settings.use_cache):
        """
        Get channel data between ``start`` and ``end`` or ``start`` and ``start + length`` 

        Args:
            start     (optional): start time of data (usecs or datetime object)
            end       (optional): end time of data (usecs or datetime object)
            length    (optional): length of data to retrieve, e.g. '1s', '5s', '10m', '1h'
            use_cache (optional): whether to use locally cached data

        Returns:
            Pandas Series containing requested data for channel.

        Note:
            Data requests will be automatically chunked and combined into a single Pandas
            Series. However, you must be sure you request only a span of data that
            will properly fit in memory. 

            See ``get_data_iter`` for an iterator approach to timeseries data retrieval.

        Example:

            Get 5 seconds of data from start over all channels::

                data = channel.get_data(length='5s')

            Get data betwen 12345 and 56789 (representing usecs since Epoch)::

                data = channel.get_data(start=12345, end=56789)
        """

        return self._api.timeseries.get_ts_data(
                ts         = self._pkg,
                start      = start,
                end        = end,
                length     = length,
                channels   = [self],
                use_cache  = use_cache)

    def get_data_iter(self, start=None, end=None, length=None, chunk_size=None, use_cache=settings.use_cache):
        """
        Returns iterator over the data. Must specify **either ``end`` OR ``length``**, not both.

        Args:
            start      (optional): start time of data (default: earliest time available).
            end        (optional): end time of data (default: latest time avialable).
            length     (optional): some time length, e.g. '1s', '5m', '1h' or number of usecs
            chunk_size (optional): some time length, e.g. '1s', '5m', '1h' or number of usecs
            use_cache  (optional): whether to use locally cached data

        Returns:
            Iterator of Pandas Series, each the size of ``chunk_size``.
        """

        return self._api.timeseries.get_ts_data_iter(
                ts         = self._pkg,
                start      = start,
                end        = end,
                length     = length,
                channels   = [self],
                chunk_size = chunk_size,
                use_cache  = use_cache)

    def as_dict(self):
        return {
            "name": self.name,
            "start": self.start,
            "end": self.end,
            "unit": self.unit,
            "rate": self.rate,
            "channelType": self.channel_type,
            "lastAnnotation": self.last_annot,
            "group": self.group,
            "spikeDuration": self.spike_duration,
            "properties": [x.as_dict() for x in self.properties]
        }

    def __repr__(self):
        return u"<TimeSeriesChannel name=\'{}\' id=\'{}\'>".format(self.name, self.id)


class TimeSeriesAnnotationLayer(BaseNode):
    """
    Annotation layer containing one or more annotations. Layers are used
    to separate annotations into logically distinct groups when applied
    to the same data package. 

    Args:
        name:           Name of the layer
        time_series_id: The TimeSeries ID which the layer applies
        description:    Description of the layer

    """
    _object_key = None

    def __init__(self, name, time_series_id, description=None, **kwargs):
        super(TimeSeriesAnnotationLayer,self).__init__(**kwargs)
        self.name = name
        self.time_series_id= time_series_id
        self.description = description

    def iter_annotations(self, window_size=10, channels=None):
        """
        Iterate over annotations according to some window size (seconds).

        Args:
            window_size (float): Number of seconds in window
            channels:            List of channel objects or IDs
        
        Yields:
            List of annotations found in current window.
        """
        self._check_exists()
        ts = self._api.core.get(self.time_series_id)
        return self._api.timeseries.iter_annotations(
            ts=ts, layer=self, channels=channels, window_size=window_size)

    def add_annotations(self, annotations):
        """
        Add annotations to layer.

        Args:
            annotations (str): List of annotation objects to add.

        """
        self._check_exists()
        return self._api.timeseries.create_annotations(layer=self, annotations=annotations)

    def insert_annotation(self,annotation,start=None,end=None,channel_ids=None,description=None):
        """
        Add annotations; proxy for ``add_annotations``. 

        Args:
            annotation (str): Annotation string
            start:            Start time (usecs or datetime)
            end:              End time (usecs or datetime)
            channel_ids:      list of channel IDs

        Returns:
            The created annotation object.
        """
        self._check_exists()
        return self._api.timeseries.create_annotation(layer=self, annotation=annotation,start=start,end=end,channel_ids=channel_ids,description=description)

    def annotations(self, start=None, end=None, channels=None):
        """
        Get annotations between ``start`` and ``end`` over ``channels`` (all channels by default).


        Args:
            start:    Start time
            end:      End time
            channels: List of channel objects or IDs

        """
        self._check_exists()
        ts = self._api.core.get(self.time_series_id)
        return self._api.timeseries.query_annotations(
            ts=ts, layer=self, channels=channels, start=start, end=end)

    def annotation_counts(self, start, end, channels=None):
        """
        The number of annotations between ``start`` and ``end`` over selected 
        channels (all by default).

        Args:
            start:    Start time
            end:      End time
            channels: List of channel objects or IDs
        """
        self._check_exists()
        ts = self._api.core.get(self.time_series_id)
        return self._api.timeseries.query_annotation_counts(
            ts=ts, layer=self, channels=channels, start=start, end=end)

    def delete(self):
        """
        Delete annotation layer.
        """
        self._check_exists()
        return self._api.timeseries.delete_annotation_layer(self)

    def as_dict(self):
        return {
            "name" : self.name,
            "description" : self.description
        }

    def __repr__(self):
        return u"<TimeSeriesAnnotationLayer name=\'{}\' id=\'{}\'>".format(self.name, self.id)


class TimeSeriesAnnotation(BaseNode):
    """
    Annotation is an event on one or more channels in a dataset

    Args:
        label (str):    The label for the annotation
        channel_ids:    List of channel IDs that annotation applies
        start:          Start time
        end:            End time
        name:           Name of annotation
        layer_id:       Layer ID for annoation (all annotations exist on a layer)
        time_series_id: TimeSeries package ID
        description:    Description of annotation

    """
    _object_key = None


    def __init__(self, label, channel_ids, start, end, name='',layer_id= None, 
                 time_series_id = None, description=None, **kwargs):
        self.user_id = kwargs.pop('userId', None)
        super(TimeSeriesAnnotation,self).__init__(**kwargs)
        self.name = ''
        self.label = label
        self.channel_ids = channel_ids
        self.start = start
        self.end = end
        self.description = description
        self.layer_id = layer_id
        self.time_series_id = time_series_id

    def delete(self):
        self._check_exists()
        return self._api.timeseries.delete_annotation(annot=self)

    def as_dict(self):
        channel_ids = self.channel_ids
        if type(channel_ids) is not list:
            channel_ids = [channel_ids]
        return {
            "name" : self.name,
            "label" : self.label, 
            "channelIds": channel_ids,
            "start" : self.start, 
            "end" : self.end, 
            "description" : self.description, 
            "layer_id" : self.layer_id, 
            "time_series_id" : self.time_series_id,
        }

    def __repr__(self):
        date = datetime.datetime.fromtimestamp(self.start/1e6)
        return u"<TimeSeriesAnnotation label=\'{}\' layer=\'{}\' start=\'{}\'>".format(self.label, self.layer_id, date.isoformat())


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# Tabular
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

class Tabular(DataPackage):
    """
    Represents a Tabular package on the platform.

    Args:
        name: The name of the package
    """
    def __init__(self, name, **kwargs):
        kwargs.pop('package_type',None)
        super(Tabular,self).__init__(
            name=name,
            package_type="Tabular",
            **kwargs)
        self.schema = None

    def get_data(self,limit=1000, offset=0,order_by = None, order_direction='ASC'):
        """
        Get data from tabular package as DataFrame

        Args:
            limit:           Max number of rows to return (1000 default)
            offset:          Offset when retrieving rows
            order_by:        Column to order data
            order_direction: Ascending ('ASC') or descending ('DESC')

        Returns:
            Pandas DataFrame

        """
        self._check_exists()
        return self._api.tabular.get_tabular_data(self,limit=limit,offset=offset ,order_by=order_by, order_direction=order_direction)

    def get_data_iter(self, chunk_size=10000, offset=0, order_by = None, order_direction='ASC'):
        """
        Iterate over tabular data, each data chunk will be of size ``chunk_size``.
        """
        self._check_exists()
        return self._api.tabular.get_tabular_data_iter(self,chunk_size=chunk_size,offset=offset,order_by=order_by, order_direction=order_direction)

    def set_schema(self, schema):
        self.schema = schema
        # TODO: parse response
        return self._api.tabular.set_table_schema(self, schema)

    def get_schema(self):
        self._check_exists()
        # TODO: parse response
        return self._api.tabular.get_table_schema(self)

    def __repr__(self):
        return u"<Tabular name=\'{}\' id=\'{}\'>".format(self.name, self.id)


class TabularSchema(BaseNode):
    def __init__(self, name, column_schema = [], **kwargs):
        super(TabularSchema, self).__init__(**kwargs)
        self.name = name
        self.column_schema = column_schema

    @classmethod
    def from_dict(cls, data):
        column_schema = []
        for x in data['columns']:
            if 'displayName' not in x.keys():
                x['displayName'] = ''
            column_schema.append(TabularSchemaColumn.from_dict(x))

        return cls(
            name = data['name'],
            id = data['id'],
            column_schema = column_schema
           )

    def as_dict(self):
        column_schema = [dict(
            name = x.name,
            displayName = x.display_name,
            datatype = x.datatype,
            primaryKey = x.primary_key,
            internal = x.internal
        ) for x in self.column_schema]
        return column_schema

    def __repr__(self):
        return u"<TabularSchema name=\'{}\' id=\'{}\'>".format(self.name, self.id)

class TabularSchemaColumn():

    def __init__(self, name, display_name, datatype, primary_key = False, internal = False, **kwargs):
        self.name=name
        self.display_name = display_name
        self.datatype = datatype
        self.internal = internal
        self.primary_key = primary_key

    @classmethod
    def from_dict(cls, data):
        return cls(
            name = data['name'],
            display_name = data['displayName'],
            datatype = data['datatype'],
            primary_key = data['primaryKey'],
            internal = data['internal']          
        )

    def __repr__(self):
        return u"<TabularSchemaColumn name='{}' display='{}' is-primary='{}'>".format(self.name, self.display_name, self.primary_key)


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# User
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

class User(BaseNode):

    _object_key = ''

    def __init__(self,
            email,
            first_name,
            last_name,
            credential='',
            photo_url='',
            url='',
            authy_id=0,
            accepted_terms='',
            color=None,
            is_super_admin=False,
            *args,
            **kwargs):
        kwargs.pop('preferredOrganization', None)
        self.storage = kwargs.pop('storage', None)
        super(User, self).__init__(*args, **kwargs)

        self.email = email
        self.first_name = first_name
        self.last_name = last_name
        self.credential = credential
        self.photo_url = photo_url
        self.color = color
        self.url = url
        self.authy_id = authy_id
        self.accepted_terms = ''
        self.is_super_admin = is_super_admin

    def __repr__(self):
        return u"<User email=\'{}\' id=\'{}\'>".format(self.email, self.id)


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# Organizations
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

class Organization(BaseNode):
    _object_key = 'organization'

    def __init__(self,
            name,
            encryption_key_id="", 
            slug=None,
            terms=None,
            features=None,
            subscription_state=None,
            *args, **kwargs):
        self.storage = kwargs.pop('storage', None)
        super(Organization, self).__init__(*args, **kwargs)

        self.name = name
        self.terms = terms
        self.feature = features or []
        self.subscription_state = subscription_state
        self.encryption_key_id = encryption_key_id
        self.slug = name.lower().replace(' ','-') if slug is None else slug

    @property
    def datasets(self):
        """
        Return all datasets for user for an organization (current context).
        """
        self._check_exists()
        return self._api.datasets.get_all()

    @property
    def members(self):
        return self._api.organizations.get_members(self)

    def __repr__(self):
        return u"<Organization name=\'{}\' id=\'{}\'>".format(self.name, self.id)

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# Datasets
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

class Dataset(BaseCollection):
    def __init__(self, name, description=None, **kwargs):
        kwargs.pop('package_type', None)
        super(Dataset, self).__init__(name, "DataSet", **kwargs)
        self.description = description or ''

        # remove things that do not apply (a bit hacky)
        for k in ("parent", "type", "set_ready", "set_unavailable", "set_error", "state", "dataset"):
            self.__dict__.pop(k, None)

    def __repr__(self):
        return u"<Dataset name='{}' id='{}'>".format(self.name, self.id)

    @property
    def collaborators(self):
        """
        List of collaborators on Dataset.
        """
        self._check_exists()
        return self._api.datasets.get_collaborators(self)

    def add_collaborators(self, *collaborator_ids):
        """
        Add new collaborator(s) to Dataset.

        Args:
            collaborator_ids: List of collaborator IDs to add (Users, Organizations, Teams)
        """
        self._check_exists()
        return self._api.datasets.add_collaborators(self, *collaborator_ids)

    def remove_collaborators(self, *collaborator_ids):
        """
        Remove collaborator(s) from Dataset.

        Args:
            collaborator_ids: List of collaborator IDs to remove (Users)
        """
        self._check_exists()
        return self._api.datasets.remove_collaborators(self, *collaborator_ids)

    @property
    def _get_method(self):
        return self._api.datasets.get

    def as_dict(self):
        return dict(
            name = self.name,
            description = self.description,
            properties = [p.as_dict() for p in self.properties]
        )

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# Collections
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

class Collection(BaseCollection):
    def __init__(self, name, **kwargs):
        kwargs.pop('package_type', None)
        super(Collection, self).__init__(name, package_type="Collection", **kwargs)

    @property
    def _get_method(self):
        return self._api.packages.get

    def __repr__(self):
        return u"<Collection name='{}' id='{}'>".format(self.name, self.id)


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# Data Ledger
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

class LedgerEntry(BaseNode):
    def __init__(self,
            reference,
            userId,
            organizationId,
            metric,
            value,
            date):

        super(LedgerEntry, self).__init__()
        self.reference = reference
        self.userId = userId
        self.organizationId = organizationId
        self.metric = metric
        self.value = value
        self.date = date

    @classmethod
    def from_dict(self, data):
        return LedgerEntry(data["reference"],
                data["userId"],
                data["organizationId"],
                data["metric"],
                data["value"],
                dateutil.parser.parse(data["date"]))

    def as_dict(self):
        return {
                "reference": self.reference,
                "userId": self.userId,
                "organizationId": self.organizationId,
                "metric": self.metric,
                "value": self.value,
                "date": self.date.replace(microsecond=0).isoformat() + 'Z'
                }


