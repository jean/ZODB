
import os
import time
import tempfile

from zope.interface import implements

from ZODB.Blobs.interfaces import IBlob
from ZODB.Blobs.exceptions import BlobError
from ZODB import utils
import transaction
from transaction.interfaces import IDataManager
from persistent import Persistent

class Blob(Persistent):
 
    implements(IBlob)

    _p_blob_readers = 0
    _p_blob_writers = 0
    _p_blob_uncommitted = None
    _p_blob_data = None

    # All persistent object store a reference to their data manager, a database
    # connection in the _p_jar attribute. So we are going to do the same with
    # blobs here.
    _p_blob_manager = None

    def open(self, mode="r"):
        """ Returns a file(-like) object representing blob data.  This
        method will either return the file object, raise a BlobError
        or an IOError.  A file may be open for exclusive read any
        number of times, but may not be opened simultaneously for read
        and write during the course of a single transaction and may
        not be opened for simultaneous writes during the course of a
        single transaction. Additionally, the file handle which
        results from this method call is unconditionally closed at
        transaction boundaries and so may not be used across
        transactions.  """
        
        result = None

        if (mode.startswith("r") or mode=="U"):
            if self._current_filename() is None:
                raise BlobError, "Blob does not exist."

            if self._p_blob_writers != 0:
                raise BlobError, "Already opened for writing."

            self._p_blob_readers += 1
            result = BlobFile(self._current_filename(), mode, self)

        elif mode.startswith("w"):
            if self._p_blob_readers != 0:
                raise BlobError, "Already opened for reading."

            if self._p_blob_uncommitted is None:
                self._p_blob_uncommitted = utils.mktemp()

            self._p_blob_writers += 1
            result = BlobFile(self._p_blob_uncommitted, mode, self)

        elif mode.startswith("a"):
            if self._p_blob_readers != 0:
                raise BlobError, "Already opened for reading."

            if self._p_blob_uncommitted is None:
                # Create a new working copy
                self._p_blob_uncommitted = utils.mktemp()
                uncommitted = BlobFile(self._p_blob_uncommitted, mode, self)
                # NOTE: _p_blob data appears by virtue of Connection._setstate
                utils.cp(file(self._p_blob_data), uncommitted)
                uncommitted.seek(0)
            else:
                # Re-use existing working copy
                uncommitted = BlobFile(self._p_blob_uncommitted, mode, self)

            self._p_blob_writers +=1
            result = uncommitted

        else:
            raise IOError, 'invalid mode: %s ' % mode

        if result is not None:

            # we register ourselves as a data manager with the
            # transaction machinery in order to be notified of
            # commit/vote/abort events.  We do this because at
            # transaction boundaries, we need to fix up _p_ reference
            # counts that keep track of open readers and writers and
            # close any writable filehandles we've opened.

            if self._p_blob_manager is None:
                dm = BlobDataManager(self, result)
                transaction.get().register(dm)
            else:
                # each blob data manager should manage only the one blob
                # assert that this is the case and it is the correct blob
                assert self._p_blob_manager.blob is self
                self._p_blob_manager.register_fh(result)

        return result

    # utility methods

    def _current_filename(self):
        # NOTE: _p_blob_data and _p_blob_uncommitted appear by virtue of
        # Connection._setstate
        return self._p_blob_uncommitted or self._p_blob_data

    def _change(self):
        self._p_changed = 1

    # utility methods which should not cause the object's state to be
    # loaded if they are called while the object is a ghost.  Thus,
    # they are named with the _p_ convention and only operate against
    # other _p_ instance attributes. We conventionally name these methods
    # and attributes with a _p_blob prefix.

    def _p_blob_clear(self):
        self._p_blob_readers = 0
        self._p_blob_writers = 0

    def _p_blob_decref(self, mode):
        if mode.startswith('r') or mode == 'U':
            self._p_blob_readers = max(0, self._p_blob_readers - 1)
        elif mode.startswith('w') or mode.startswith('a'):
            self._p_blob_writers = max(0, self._p_blob_writers - 1)
        else:
            raise AssertionError, 'Unknown mode %s' % mode

    def _p_blob_refcounts(self):
        # used by unit tests
        return self._p_blob_readers, self._p_blob_writers

class BlobDataManager:
    """Special data manager to handle transaction boundaries for blobs.

    Blobs need some special care-taking on transaction boundaries. As

    a) the ghost objects might get reused, the _p_ reader and writer
       refcount attributes must be set to a consistent state
    b) the file objects might get passed out of the thread/transaction
       and must deny any relationship to the original blob.
    c) writable blob filehandles must be closed at the end of a txn so
       as to not allow reuse between two transactions.

    """

    implements(IDataManager)

    def __init__(self, blob, filehandle):
        self.blob = blob
        # we keep a weakref to the file handle because we don't want to
        # keep it alive if all other references to it die (e.g. in the
        # case it's opened without assigning it to a name).
        self.fhrefs = utils.WeakSet()
        self.register_fh(filehandle)
        self.subtransaction = False
        self.sortkey = time.time()

    def register_fh(self, filehandle):
        self.fhrefs.add(filehandle)

    def abort_sub(self, transaction):
        pass

    def commit_sub(self, transaction):
        pass

    def tpc_begin(self, transaction, subtransaction=False):
        self.subtransaction = subtransaction

    def tpc_abort(self, transaction):
        pass

    def tpc_finish(self, transaction):
        self.subtransaction = False

    def tpc_vote(self, transaction):
        pass
                
    def commit(self, object, transaction):
        if not self.subtransaction:
            self.blob._p_blob_clear() # clear all blob refcounts
            self.fhrefs.map(lambda fhref: fhref.close())

    def abort(self, object, transaction):
        if not self.subtransaction:
            self.blob._p_blob_clear()
            self.fhrefs.map(lambda fhref: fhref.close())
            if self.blob._p_blob_uncommitted is not None and \
               os.path.exists(self.blob._p_blob_uncommitted):
                os.unlink(self.blob._p_blob_uncommitted)

    def sortKey(self):
        return self.sortkey

    def beforeCompletion(self, transaction):
        pass

    def afterCompletion(self, transaction):
        pass

class BlobFile(file):
    
    """ A BlobFile is a file that can be used within a transaction
    boundary; a BlobFile is just a Python file object, we only
    override methods which cause a change to blob data in order to
    call methods on our 'parent' persistent blob object signifying
    that the change happened. """

    # XXX these files should be created in the same partition as
    # the storage later puts them to avoid copying them ...

    def __init__(self, name, mode, blob):
        super(BlobFile, self).__init__(name, mode)
        self.blob = blob
        self.close_called = False

    def write(self, data):
        super(BlobFile, self).write(data)
        self.blob._change()

    def writelines(self, lines):
        super(BlobFile, self).writelines(lines)
        self.blob._change()

    def truncate(self, size=0):
        super(BlobFile, self).truncate(size)
        self.blob._change()

    def close(self):
        # we don't want to decref twice
        if not self.close_called:
            self.blob._p_blob_decref(self.mode)
            self.close_called = True
            super(BlobFile, self).close()

    def __del__(self):
        # XXX we need to ensure that the file is closed at object
        # expiration or our blob's refcount won't be decremented.
        # This probably needs some work; I don't know if the names
        # 'BlobFile' or 'super' will be available at program exit, but
        # we'll assume they will be for now in the name of not
        # muddying the code needlessly.
        self.close()
