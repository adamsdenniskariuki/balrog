from os import path
import simplejson as json

import jsonschema

import yaml

import logging

from auslib.AUS import isSpecialURL
from auslib.global_state import cache


class BlobValidationError(ValueError):
    def __init__(self, message, errors, *args, **kwargs):
        self.errors = errors
        super(BlobValidationError, self).__init__(message, *args, **kwargs)


def createBlob(data):
    """Takes a string form of a blob (eg from DB or API) and converts into an
    actual blob, taking care to notice the schema"""
    # These imports need to be done here to avoid errors due to circular
    # between this module and specific blob modules like apprelease.
    from auslib.blobs.apprelease import ReleaseBlobV1, ReleaseBlobV2, ReleaseBlobV3, \
        ReleaseBlobV4, ReleaseBlobV5, ReleaseBlobV6, ReleaseBlobV7, ReleaseBlobV8, DesupportBlob
    from auslib.blobs.gmp import GMPBlobV1
    from auslib.blobs.superblob import SuperBlob
    from auslib.blobs.systemaddons import SystemAddonsBlob

    blob_map = {
        1: ReleaseBlobV1,
        2: ReleaseBlobV2,
        3: ReleaseBlobV3,
        4: ReleaseBlobV4,
        5: ReleaseBlobV5,
        6: ReleaseBlobV6,
        7: ReleaseBlobV7,
        8: ReleaseBlobV8,
        50: DesupportBlob,
        1000: GMPBlobV1,
        4000: SuperBlob,
        5000: SystemAddonsBlob
    }

    if isinstance(data, basestring):
        data = json.loads(data)
    schema_version = data.get("schema_version")

    if not schema_version:
        raise ValueError("schema_version is not set")
    if schema_version not in blob_map:
        raise ValueError("schema_version is unknown")

    return blob_map[schema_version](**data)


def merge_lists(*lists):
    result = []
    for l in lists:
        for i in l:
            if i not in result or not isinstance(i, type(result[result.index(i)])):
                result.append(i)
    return result


def merge_dicts(ancestor, left, right):
    result = {}
    dicts = (ancestor, left, right)
    for key in set(key for d in dicts for key in d.keys()):
        key_types = set([type(d.get(key)) for d in dicts])
        key_types.discard(type(None))
        if len(key_types) > 1 and not key_types.issubset([str, unicode]):
            raise ValueError("Cannot merge blobs: type mismatch for '{}'".format(key.encode('ascii', 'replace')))

        if any(isinstance(d.get(key), dict) for d in dicts):
            result[key] = merge_dicts(*[d.get(key, {}) for d in dicts])
        elif any(isinstance(d.get(key), list) for d in dicts):
            result[key] = merge_lists(*[d.get(key, []) for d in dicts])
        else:
            if key in ancestor:
                if key in left and key in right and ancestor[key] != left[key] and ancestor[key] != right[key]:
                    raise ValueError("Cannot merge blobs: left and right are both changing '{}'".format(key.encode('ascii', 'replace')))
                if key in left and ancestor[key] != left.get(key):
                    result[key] = left[key]
                elif key in right and ancestor[key] != right.get(key):
                    result[key] = right[key]
                else:
                    result[key] = ancestor[key]
            else:
                if key in left and key in right and left[key] != right[key]:
                    raise ValueError("Cannot merge blobs: left and right are both changing '{}'".format(key.encode('ascii', 'replace')))
                if key in left:
                    result[key] = left[key]
                elif key in right:
                    result[key] = right[key]
                else:
                    raise KeyError("Couldn't find value for key '{}'".format(key))

    return result


class Blob(dict):
    jsonschema = None

    def __init__(self, *args, **kwargs):
        super(Blob, self).__init__(self, *args, **kwargs)
        # Blobs need to be pickable to go into the cache properly. Pickling
        # extendes to all instance-level attributes, and our Loggers are not
        # pickleable. Moving them to the class level avoids this issue without
        # the need for subclasses to worry about instantiating their own
        # Loggers.
        logger_name = "{0}.{1}".format(self.__class__.__module__, self.__class__.__name__)
        self.__class__.log = logging.getLogger(logger_name)

    def validate(self, product, whitelistedDomains):
        """Raises a BlobValidationError if the blob is invalid."""
        self.log.debug('Validating blob %s' % self)
        validator = jsonschema.Draft4Validator(self.getSchema())
        # Normal usage is to use .validate(), but errors raised by it return
        # a massive error message that includes the entire blob, which is way
        # too big to be useful in the UI. Instead, we iterate over the
        # individual errors (which are all single sentences which contain
        # the name of the failing property and why it failed), and return those.
        errors = [e.message for e in validator.iter_errors(self)]
        if errors:
            raise BlobValidationError("Invalid blob! See 'errors' for details.", errors)

        if self.containsForbiddenDomain(product, whitelistedDomains):
            raise ValueError("Blob contains forbidden domain(s)")

    def getResponseProducts(self):
        """
        :return: Usually returns None. If the Blob is a SuperBlob, it returns the list
                of return products.
        """
        return None

    def getResponseBlobs(self):
        """
        :return: Usually returns None. It the Blob is a systemaddons superblob, it returns the
                 list of return blobs
        """
        return None

    def getSchema(self):
        def loadSchema():
            return yaml.load(open(path.join(path.dirname(path.abspath(__file__)), "schemas", self.jsonschema)))

        return cache.get("blob_schema", self.jsonschema, loadSchema)

    def loadJSON(self, data):
        """Replaces this blob's contents with parsed contents of the json
           string provided."""
        self.clear()
        self.update(json.loads(data))

    def getJSON(self):
        """Returns a JSON formatted version of this blob."""
        return json.dumps(self)

    def shouldServeUpdate(self, updateQuery):
        """Should be implemented by subclasses. In the event that it's not,
        False is the safest thing to return (it will fail closed instead of
        failing open)."""
        return False

    def processSpecialForceHosts(self, url, specialForceHosts, force_arg):
        if isSpecialURL(url, specialForceHosts):
            if '?' in url:
                url += '&force=' + force_arg.query_value
            else:
                url += '?force=' + force_arg.query_value
        return url

    def getInnerHeaderXML(self, updateQuery, update_type, whitelistedDomains, specialForceHosts):
        """
        :return: Releases-specific header should be implemented for individual blobs
        """
        raise NotImplementedError()

    def getInnerFooterXML(self, updateQuery, update_type, whitelistedDomains, specialForceHosts):
        """
        :return: Releases-specific header should be implemented for individual blobs
        """
        raise NotImplementedError()

    def getInnerXML(self, updateQuery, update_type, whitelistedDomains, specialForceHosts):
        raise NotImplementedError()

    def containsForbiddenDomain(self, product, whitelistedDomains):
        raise NotImplementedError()

    def getHeaderXML(self):
        """
        :return: Returns the outer most header. Returns the outer most header
        """
        header = ['<?xml version="1.0"?>']
        header.append('<updates>')
        return header

    def getFooterXML(self):
        """
        :return: Returns the outer most footer. Returns the outer most header
        """
        footer = '</updates>'
        return footer

    def getReferencedReleases(self):
        """
        :return: Returns set of names of partially referenced releases that the current
        release references
        """
        return set()
