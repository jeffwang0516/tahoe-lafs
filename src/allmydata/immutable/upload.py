import os, time, weakref, itertools
from zope.interface import implements
from twisted.python import failure
from twisted.internet import defer
from twisted.application import service
from foolscap.api import Referenceable, Copyable, RemoteCopy, fireEventually

from allmydata.util.hashutil import file_renewal_secret_hash, \
     file_cancel_secret_hash, bucket_renewal_secret_hash, \
     bucket_cancel_secret_hash, plaintext_hasher, \
     storage_index_hash, plaintext_segment_hasher, convergence_hasher
from allmydata import hashtree, uri
from allmydata.storage.server import si_b2a
from allmydata.immutable import encode
from allmydata.util import base32, dictutil, idlib, log, mathutil
from allmydata.util.happinessutil import servers_of_happiness, \
                                         shares_by_server, merge_servers, \
                                         failure_message
from allmydata.util.assertutil import precondition, _assert
from allmydata.util.rrefutil import add_version_to_remote_reference
from allmydata.interfaces import IUploadable, IUploader, IUploadResults, \
     IEncryptedUploadable, RIEncryptedUploadable, IUploadStatus, \
     NoServersError, InsufficientVersionError, UploadUnhappinessError, \
     DEFAULT_MAX_SEGMENT_SIZE, IProgress
from allmydata.immutable import layout
from pycryptopp.cipher.aes import AES

from cStringIO import StringIO


# this wants to live in storage, not here
class TooFullError(Exception):
    pass

# HelperUploadResults are what we get from the Helper, and to retain
# backwards compatibility with old Helpers we can't change the format. We
# convert them into a local UploadResults upon receipt.
class HelperUploadResults(Copyable, RemoteCopy):
    # note: don't change this string, it needs to match the value used on the
    # helper, and it does *not* need to match the fully-qualified
    # package/module/class name
    typeToCopy = "allmydata.upload.UploadResults.tahoe.allmydata.com"
    copytype = typeToCopy

    # also, think twice about changing the shape of any existing attribute,
    # because instances of this class are sent from the helper to its client,
    # so changing this may break compatibility. Consider adding new fields
    # instead of modifying existing ones.

    def __init__(self):
        self.timings = {} # dict of name to number of seconds
        self.sharemap = dictutil.DictOfSets() # {shnum: set(serverid)}
        self.servermap = dictutil.DictOfSets() # {serverid: set(shnum)}
        self.file_size = None
        self.ciphertext_fetched = None # how much the helper fetched
        self.uri = None
        self.preexisting_shares = None # count of shares already present
        self.pushed_shares = None # count of shares we pushed

class UploadResults:
    implements(IUploadResults)

    def __init__(self, file_size,
                 ciphertext_fetched, # how much the helper fetched
                 preexisting_shares, # count of shares already present
                 pushed_shares, # count of shares we pushed
                 sharemap, # {shnum: set(server)}
                 servermap, # {server: set(shnum)}
                 timings, # dict of name to number of seconds
                 uri_extension_data,
                 uri_extension_hash,
                 verifycapstr):
        self._file_size = file_size
        self._ciphertext_fetched = ciphertext_fetched
        self._preexisting_shares = preexisting_shares
        self._pushed_shares = pushed_shares
        self._sharemap = sharemap
        self._servermap = servermap
        self._timings = timings
        self._uri_extension_data = uri_extension_data
        self._uri_extension_hash = uri_extension_hash
        self._verifycapstr = verifycapstr

    def set_uri(self, uri):
        self._uri = uri

    def get_file_size(self):
        return self._file_size
    def get_uri(self):
        return self._uri
    def get_ciphertext_fetched(self):
        return self._ciphertext_fetched
    def get_preexisting_shares(self):
        return self._preexisting_shares
    def get_pushed_shares(self):
        return self._pushed_shares
    def get_sharemap(self):
        return self._sharemap
    def get_servermap(self):
        return self._servermap
    def get_timings(self):
        return self._timings
    def get_uri_extension_data(self):
        return self._uri_extension_data
    def get_verifycapstr(self):
        return self._verifycapstr

# our current uri_extension is 846 bytes for small files, a few bytes
# more for larger ones (since the filesize is encoded in decimal in a
# few places). Ask for a little bit more just in case we need it. If
# the extension changes size, we can change EXTENSION_SIZE to
# allocate a more accurate amount of space.
EXTENSION_SIZE = 1000
# TODO: actual extensions are closer to 419 bytes, so we can probably lower
# this.

def pretty_print_shnum_to_servers(s):
    return ', '.join([ "sh%s: %s" % (k, '+'.join([idlib.shortnodeid_b2a(x) for x in v])) for k, v in s.iteritems() ])

class ServerTracker:
    def __init__(self, server,
                 sharesize, blocksize, num_segments, num_share_hashes,
                 storage_index,
                 bucket_renewal_secret, bucket_cancel_secret):
        self._server = server
        self.buckets = {} # k: shareid, v: IRemoteBucketWriter
        self.sharesize = sharesize

        wbp = layout.make_write_bucket_proxy(None, None, sharesize,
                                             blocksize, num_segments,
                                             num_share_hashes,
                                             EXTENSION_SIZE)
        self.wbp_class = wbp.__class__ # to create more of them
        self.allocated_size = wbp.get_allocated_size()
        self.blocksize = blocksize
        self.num_segments = num_segments
        self.num_share_hashes = num_share_hashes
        self.storage_index = storage_index

        self.renew_secret = bucket_renewal_secret
        self.cancel_secret = bucket_cancel_secret

    def __repr__(self):
        return ("<ServerTracker for server %s and SI %s>"
                % (self._server.get_name(), si_b2a(self.storage_index)[:5]))

    def get_server(self):
        return self._server
    def get_serverid(self):
        return self._server.get_serverid()
    def get_name(self):
        return self._server.get_name()

    def query(self, sharenums):
        rref = self._server.get_rref()
        d = rref.callRemote("allocate_buckets",
                            self.storage_index,
                            self.renew_secret,
                            self.cancel_secret,
                            sharenums,
                            self.allocated_size,
                            canary=Referenceable())
        d.addCallback(self._got_reply)
        return d

    def ask_about_existing_shares(self):
        rref = self._server.get_rref()
        return rref.callRemote("get_buckets", self.storage_index)

    def _got_reply(self, (alreadygot, buckets)):
        #log.msg("%s._got_reply(%s)" % (self, (alreadygot, buckets)))
        b = {}
        for sharenum, rref in buckets.iteritems():
            bp = self.wbp_class(rref, self._server, self.sharesize,
                                self.blocksize,
                                self.num_segments,
                                self.num_share_hashes,
                                EXTENSION_SIZE)
            b[sharenum] = bp
        self.buckets.update(b)
        return (alreadygot, set(b.keys()))


    def abort(self):
        """
        I abort the remote bucket writers for all shares. This is a good idea
        to conserve space on the storage server.
        """
        self.abort_some_buckets(self.buckets.keys())

    def abort_some_buckets(self, sharenums):
        """
        I abort the remote bucket writers for the share numbers in sharenums.
        """
        for sharenum in sharenums:
            if sharenum in self.buckets:
                self.buckets[sharenum].abort()
                del self.buckets[sharenum]


def str_shareloc(shnum, bucketwriter):
    return "%s: %s" % (shnum, bucketwriter.get_servername(),)

class Tahoe2ServerSelector(log.PrefixingLogMixin):

    def __init__(self, upload_id, logparent=None, upload_status=None):
        self.upload_id = upload_id
        self.query_count, self.good_query_count, self.bad_query_count = 0,0,0
        # Servers that are working normally, but full.
        self.full_count = 0
        self.error_count = 0
        self.num_servers_contacted = 0
        self.last_failure_msg = None
        self._status = IUploadStatus(upload_status)
        log.PrefixingLogMixin.__init__(self, 'tahoe.immutable.upload', logparent, prefix=upload_id)
        self.log("starting", level=log.OPERATIONAL)

    def __repr__(self):
        return "<Tahoe2ServerSelector for upload %s>" % self.upload_id

    def get_shareholders(self, storage_broker, secret_holder,
                         storage_index, share_size, block_size,
                         num_segments, total_shares, needed_shares,
                         servers_of_happiness):
        """
        @return: (upload_trackers, already_serverids), where upload_trackers
                 is a set of ServerTracker instances that have agreed to hold
                 some shares for us (the shareids are stashed inside the
                 ServerTracker), and already_serverids is a dict mapping
                 shnum to a set of serverids for servers which claim to
                 already have the share.
        """

        if self._status:
            self._status.set_status("Contacting Servers..")

        self.total_shares = total_shares
        self.servers_of_happiness = servers_of_happiness
        self.needed_shares = needed_shares

        self.homeless_shares = set(range(total_shares))
        self.use_trackers = set() # ServerTrackers that have shares assigned
                                  # to them
        self.preexisting_shares = {} # shareid => set(serverids) holding shareid

        # These servers have shares -- any shares -- for our SI. We keep
        # track of these to write an error message with them later.
        self.serverids_with_shares = set()

        # this needed_hashes computation should mirror
        # Encoder.send_all_share_hash_trees. We use an IncompleteHashTree
        # (instead of a HashTree) because we don't require actual hashing
        # just to count the levels.
        ht = hashtree.IncompleteHashTree(total_shares)
        num_share_hashes = len(ht.needed_hashes(0, include_leaf=True))

        # figure out how much space to ask for
        wbp = layout.make_write_bucket_proxy(None, None,
                                             share_size, 0, num_segments,
                                             num_share_hashes, EXTENSION_SIZE)
        allocated_size = wbp.get_allocated_size()
        all_servers = storage_broker.get_servers_for_psi(storage_index)
        if not all_servers:
            raise NoServersError("client gave us zero servers")

        # filter the list of servers according to which ones can accomodate
        # this request. This excludes older servers (which used a 4-byte size
        # field) from getting large shares (for files larger than about
        # 12GiB). See #439 for details.
        def _get_maxsize(server):
            v0 = server.get_rref().version
            v1 = v0["http://allmydata.org/tahoe/protocols/storage/v1"]
            return v1["maximum-immutable-share-size"]
        writeable_servers = [server for server in all_servers
                            if _get_maxsize(server) >= allocated_size]
        readonly_servers = set(all_servers[:2*total_shares]) - set(writeable_servers)

        # decide upon the renewal/cancel secrets, to include them in the
        # allocate_buckets query.
        client_renewal_secret = secret_holder.get_renewal_secret()
        client_cancel_secret = secret_holder.get_cancel_secret()

        file_renewal_secret = file_renewal_secret_hash(client_renewal_secret,
                                                       storage_index)
        file_cancel_secret = file_cancel_secret_hash(client_cancel_secret,
                                                     storage_index)
        def _make_trackers(servers):
            trackers = []
            for s in servers:
                seed = s.get_lease_seed()
                renew = bucket_renewal_secret_hash(file_renewal_secret, seed)
                cancel = bucket_cancel_secret_hash(file_cancel_secret, seed)
                st = ServerTracker(s,
                                   share_size, block_size,
                                   num_segments, num_share_hashes,
                                   storage_index,
                                   renew, cancel)
                trackers.append(st)
            return trackers

        # We assign each servers/trackers into one three lists. They all
        # start in the "first pass" list. During the first pass, as we ask
        # each one to hold a share, we move their tracker to the "second
        # pass" list, until the first-pass list is empty. Then during the
        # second pass, as we ask each to hold more shares, we move their
        # tracker to the "next pass" list, until the second-pass list is
        # empty. Then we move everybody from the next-pass list back to the
        # second-pass list and repeat the "second" pass (really the third,
        # fourth, etc pass), until all shares are assigned, or we've run out
        # of potential servers.
        self.first_pass_trackers = _make_trackers(writeable_servers)
        self.second_pass_trackers = [] # servers worth asking again
        self.next_pass_trackers = [] # servers that we have asked again
        self._started_second_pass = False

        # We don't try to allocate shares to these servers, since they've
        # said that they're incapable of storing shares of the size that we'd
        # want to store. We ask them about existing shares for this storage
        # index, which we want to know about for accurate
        # servers_of_happiness accounting, then we forget about them.
        readonly_trackers = _make_trackers(readonly_servers)

        # We now ask servers that can't hold any new shares about existing
        # shares that they might have for our SI. Once this is done, we
        # start placing the shares that we haven't already accounted
        # for.
        ds = []
        if self._status and readonly_trackers:
            self._status.set_status("Contacting readonly servers to find "
                                    "any existing shares")
        for tracker in readonly_trackers:
            assert isinstance(tracker, ServerTracker)
            d = tracker.ask_about_existing_shares()
            d.addBoth(self._handle_existing_response, tracker)
            ds.append(d)
            self.num_servers_contacted += 1
            self.query_count += 1
            self.log("asking server %s for any existing shares" %
                     (tracker.get_name(),), level=log.NOISY)
        dl = defer.DeferredList(ds)
        dl.addCallback(lambda ign: self._loop())
        return dl


    def _handle_existing_response(self, res, tracker):
        """
        I handle responses to the queries sent by
        Tahoe2ServerSelector._existing_shares.
        """
        serverid = tracker.get_serverid()
        if isinstance(res, failure.Failure):
            self.log("%s got error during existing shares check: %s"
                    % (tracker.get_name(), res), level=log.UNUSUAL)
            self.error_count += 1
            self.bad_query_count += 1
        else:
            buckets = res
            if buckets:
                self.serverids_with_shares.add(serverid)
            self.log("response to get_buckets() from server %s: alreadygot=%s"
                    % (tracker.get_name(), tuple(sorted(buckets))),
                    level=log.NOISY)
            for bucket in buckets:
                self.preexisting_shares.setdefault(bucket, set()).add(serverid)
                self.homeless_shares.discard(bucket)
            self.full_count += 1
            self.bad_query_count += 1


    def _get_progress_message(self):
        if not self.homeless_shares:
            msg = "placed all %d shares, " % (self.total_shares)
        else:
            msg = ("placed %d shares out of %d total (%d homeless), " %
                   (self.total_shares - len(self.homeless_shares),
                    self.total_shares,
                    len(self.homeless_shares)))
        return (msg + "want to place shares on at least %d servers such that "
                      "any %d of them have enough shares to recover the file, "
                      "sent %d queries to %d servers, "
                      "%d queries placed some shares, %d placed none "
                      "(of which %d placed none due to the server being"
                      " full and %d placed none due to an error)" %
                        (self.servers_of_happiness, self.needed_shares,
                         self.query_count, self.num_servers_contacted,
                         self.good_query_count, self.bad_query_count,
                         self.full_count, self.error_count))


    def _loop(self):
        if not self.homeless_shares:
            merged = merge_servers(self.preexisting_shares, self.use_trackers)
            effective_happiness = servers_of_happiness(merged)
            if self.servers_of_happiness <= effective_happiness:
                msg = ("server selection successful for %s: %s: pretty_print_merged: %s, "
                       "self.use_trackers: %s, self.preexisting_shares: %s") \
                       % (self, self._get_progress_message(),
                          pretty_print_shnum_to_servers(merged),
                          [', '.join([str_shareloc(k,v)
                                      for k,v in st.buckets.iteritems()])
                           for st in self.use_trackers],
                          pretty_print_shnum_to_servers(self.preexisting_shares))
                self.log(msg, level=log.OPERATIONAL)
                return (self.use_trackers, self.preexisting_shares)
            else:
                # We're not okay right now, but maybe we can fix it by
                # redistributing some shares. In cases where one or two
                # servers has, before the upload, all or most of the
                # shares for a given SI, this can work by allowing _loop
                # a chance to spread those out over the other servers,
                delta = self.servers_of_happiness - effective_happiness
                shares = shares_by_server(self.preexisting_shares)
                # Each server in shares maps to a set of shares stored on it.
                # Since we want to keep at least one share on each server
                # that has one (otherwise we'd only be making
                # the situation worse by removing distinct servers),
                # each server has len(its shares) - 1 to spread around.
                shares_to_spread = sum([len(list(sharelist)) - 1
                                        for (server, sharelist)
                                        in shares.items()])
                if delta <= len(self.first_pass_trackers) and \
                   shares_to_spread >= delta:
                    items = shares.items()
                    while len(self.homeless_shares) < delta:
                        # Loop through the allocated shares, removing
                        # one from each server that has more than one
                        # and putting it back into self.homeless_shares
                        # until we've done this delta times.
                        server, sharelist = items.pop()
                        if len(sharelist) > 1:
                            share = sharelist.pop()
                            self.homeless_shares.add(share)
                            self.preexisting_shares[share].remove(server)
                            if not self.preexisting_shares[share]:
                                del self.preexisting_shares[share]
                            items.append((server, sharelist))
                        for writer in self.use_trackers:
                            writer.abort_some_buckets(self.homeless_shares)
                    return self._loop()
                else:
                    # Redistribution won't help us; fail.
                    server_count = len(self.serverids_with_shares)
                    failmsg = failure_message(server_count,
                                              self.needed_shares,
                                              self.servers_of_happiness,
                                              effective_happiness)
                    servmsgtempl = "server selection unsuccessful for %r: %s (%s), merged=%s"
                    servmsg = servmsgtempl % (
                        self,
                        failmsg,
                        self._get_progress_message(),
                        pretty_print_shnum_to_servers(merged)
                        )
                    self.log(servmsg, level=log.INFREQUENT)
                    return self._failed("%s (%s)" % (failmsg, self._get_progress_message()))

        if self.first_pass_trackers:
            tracker = self.first_pass_trackers.pop(0)
            # TODO: don't pre-convert all serverids to ServerTrackers
            assert isinstance(tracker, ServerTracker)

            shares_to_ask = set(sorted(self.homeless_shares)[:1])
            self.homeless_shares -= shares_to_ask
            self.query_count += 1
            self.num_servers_contacted += 1
            if self._status:
                self._status.set_status("Contacting Servers [%s] (first query),"
                                        " %d shares left.."
                                        % (tracker.get_name(),
                                           len(self.homeless_shares)))
            d = tracker.query(shares_to_ask)
            d.addBoth(self._got_response, tracker, shares_to_ask,
                      self.second_pass_trackers)
            return d
        elif self.second_pass_trackers:
            # ask a server that we've already asked.
            if not self._started_second_pass:
                self.log("starting second pass",
                        level=log.NOISY)
                self._started_second_pass = True
            num_shares = mathutil.div_ceil(len(self.homeless_shares),
                                           len(self.second_pass_trackers))
            tracker = self.second_pass_trackers.pop(0)
            shares_to_ask = set(sorted(self.homeless_shares)[:num_shares])
            self.homeless_shares -= shares_to_ask
            self.query_count += 1
            if self._status:
                self._status.set_status("Contacting Servers [%s] (second query),"
                                        " %d shares left.."
                                        % (tracker.get_name(),
                                           len(self.homeless_shares)))
            d = tracker.query(shares_to_ask)
            d.addBoth(self._got_response, tracker, shares_to_ask,
                      self.next_pass_trackers)
            return d
        elif self.next_pass_trackers:
            # we've finished the second-or-later pass. Move all the remaining
            # servers back into self.second_pass_trackers for the next pass.
            self.second_pass_trackers.extend(self.next_pass_trackers)
            self.next_pass_trackers[:] = []
            return self._loop()
        else:
            # no more servers. If we haven't placed enough shares, we fail.
            merged = merge_servers(self.preexisting_shares, self.use_trackers)
            effective_happiness = servers_of_happiness(merged)
            if effective_happiness < self.servers_of_happiness:
                msg = failure_message(len(self.serverids_with_shares),
                                      self.needed_shares,
                                      self.servers_of_happiness,
                                      effective_happiness)
                msg = ("server selection failed for %s: %s (%s)" %
                       (self, msg, self._get_progress_message()))
                if self.last_failure_msg:
                    msg += " (%s)" % (self.last_failure_msg,)
                self.log(msg, level=log.UNUSUAL)
                return self._failed(msg)
            else:
                # we placed enough to be happy, so we're done
                if self._status:
                    self._status.set_status("Placed all shares")
                msg = ("server selection successful (no more servers) for %s: %s: %s" % (self,
                            self._get_progress_message(), pretty_print_shnum_to_servers(merged)))
                self.log(msg, level=log.OPERATIONAL)
                return (self.use_trackers, self.preexisting_shares)

    def _got_response(self, res, tracker, shares_to_ask, put_tracker_here):
        if isinstance(res, failure.Failure):
            # This is unusual, and probably indicates a bug or a network
            # problem.
            self.log("%s got error during server selection: %s" % (tracker, res),
                    level=log.UNUSUAL)
            self.error_count += 1
            self.bad_query_count += 1
            self.homeless_shares |= shares_to_ask
            if (self.first_pass_trackers
                or self.second_pass_trackers
                or self.next_pass_trackers):
                # there is still hope, so just loop
                pass
            else:
                # No more servers, so this upload might fail (it depends upon
                # whether we've hit servers_of_happiness or not). Log the last
                # failure we got: if a coding error causes all servers to fail
                # in the same way, this allows the common failure to be seen
                # by the uploader and should help with debugging
                msg = ("last failure (from %s) was: %s" % (tracker, res))
                self.last_failure_msg = msg
        else:
            (alreadygot, allocated) = res
            self.log("response to allocate_buckets() from server %s: alreadygot=%s, allocated=%s"
                    % (tracker.get_name(),
                       tuple(sorted(alreadygot)), tuple(sorted(allocated))),
                    level=log.NOISY)
            progress = False
            for s in alreadygot:
                self.preexisting_shares.setdefault(s, set()).add(tracker.get_serverid())
                if s in self.homeless_shares:
                    self.homeless_shares.remove(s)
                    progress = True
                elif s in shares_to_ask:
                    progress = True

            # the ServerTracker will remember which shares were allocated on
            # that peer. We just have to remember to use them.
            if allocated:
                self.use_trackers.add(tracker)
                progress = True

            if allocated or alreadygot:
                self.serverids_with_shares.add(tracker.get_serverid())

            not_yet_present = set(shares_to_ask) - set(alreadygot)
            still_homeless = not_yet_present - set(allocated)

            if progress:
                # They accepted at least one of the shares that we asked
                # them to accept, or they had a share that we didn't ask
                # them to accept but that we hadn't placed yet, so this
                # was a productive query
                self.good_query_count += 1
            else:
                self.bad_query_count += 1
                self.full_count += 1

            if still_homeless:
                # In networks with lots of space, this is very unusual and
                # probably indicates an error. In networks with servers that
                # are full, it is merely unusual. In networks that are very
                # full, it is common, and many uploads will fail. In most
                # cases, this is obviously not fatal, and we'll just use some
                # other servers.

                # some shares are still homeless, keep trying to find them a
                # home. The ones that were rejected get first priority.
                self.homeless_shares |= still_homeless
                # Since they were unable to accept all of our requests, so it
                # is safe to assume that asking them again won't help.
            else:
                # if they *were* able to accept everything, they might be
                # willing to accept even more.
                put_tracker_here.append(tracker)

        # now loop
        return self._loop()


    def _failed(self, msg):
        """
        I am called when server selection fails. I first abort all of the
        remote buckets that I allocated during my unsuccessful attempt to
        place shares for this file. I then raise an
        UploadUnhappinessError with my msg argument.
        """
        for tracker in self.use_trackers:
            assert isinstance(tracker, ServerTracker)
            tracker.abort()
        raise UploadUnhappinessError(msg)


class EncryptAnUploadable:
    """This is a wrapper that takes an IUploadable and provides
    IEncryptedUploadable."""
    implements(IEncryptedUploadable)
    CHUNKSIZE = 50*1024

    def __init__(self, original, log_parent=None, progress=None):
        precondition(original.default_params_set,
                     "set_default_encoding_parameters not called on %r before wrapping with EncryptAnUploadable" % (original,))
        self.original = IUploadable(original)
        self._log_number = log_parent
        self._encryptor = None
        self._plaintext_hasher = plaintext_hasher()
        self._plaintext_segment_hasher = None
        self._plaintext_segment_hashes = []
        self._encoding_parameters = None
        self._file_size = None
        self._ciphertext_bytes_read = 0
        self._status = None
        self._progress = progress

    def set_upload_status(self, upload_status):
        self._status = IUploadStatus(upload_status)
        self.original.set_upload_status(upload_status)

    def log(self, *args, **kwargs):
        if "facility" not in kwargs:
            kwargs["facility"] = "upload.encryption"
        if "parent" not in kwargs:
            kwargs["parent"] = self._log_number
        return log.msg(*args, **kwargs)

    def get_size(self):
        if self._file_size is not None:
            return defer.succeed(self._file_size)
        d = self.original.get_size()
        def _got_size(size):
            self._file_size = size
            if self._status:
                self._status.set_size(size)
            if self._progress:
                self._progress.set_progress_total(size)
            return size
        d.addCallback(_got_size)
        return d

    def get_all_encoding_parameters(self):
        if self._encoding_parameters is not None:
            return defer.succeed(self._encoding_parameters)
        d = self.original.get_all_encoding_parameters()
        def _got(encoding_parameters):
            (k, happy, n, segsize) = encoding_parameters
            self._segment_size = segsize # used by segment hashers
            self._encoding_parameters = encoding_parameters
            self.log("my encoding parameters: %s" % (encoding_parameters,),
                     level=log.NOISY)
            return encoding_parameters
        d.addCallback(_got)
        return d

    def _get_encryptor(self):
        if self._encryptor:
            return defer.succeed(self._encryptor)

        d = self.original.get_encryption_key()
        def _got(key):
            e = AES(key)
            self._encryptor = e

            storage_index = storage_index_hash(key)
            assert isinstance(storage_index, str)
            # There's no point to having the SI be longer than the key, so we
            # specify that it is truncated to the same 128 bits as the AES key.
            assert len(storage_index) == 16  # SHA-256 truncated to 128b
            self._storage_index = storage_index
            if self._status:
                self._status.set_storage_index(storage_index)
            return e
        d.addCallback(_got)
        return d

    def get_storage_index(self):
        d = self._get_encryptor()
        d.addCallback(lambda res: self._storage_index)
        return d

    def _get_segment_hasher(self):
        p = self._plaintext_segment_hasher
        if p:
            left = self._segment_size - self._plaintext_segment_hashed_bytes
            return p, left
        p = plaintext_segment_hasher()
        self._plaintext_segment_hasher = p
        self._plaintext_segment_hashed_bytes = 0
        return p, self._segment_size

    def _update_segment_hash(self, chunk):
        offset = 0
        while offset < len(chunk):
            p, segment_left = self._get_segment_hasher()
            chunk_left = len(chunk) - offset
            this_segment = min(chunk_left, segment_left)
            p.update(chunk[offset:offset+this_segment])
            self._plaintext_segment_hashed_bytes += this_segment

            if self._plaintext_segment_hashed_bytes == self._segment_size:
                # we've filled this segment
                self._plaintext_segment_hashes.append(p.digest())
                self._plaintext_segment_hasher = None
                self.log("closed hash [%d]: %dB" %
                         (len(self._plaintext_segment_hashes)-1,
                          self._plaintext_segment_hashed_bytes),
                         level=log.NOISY)
                self.log(format="plaintext leaf hash [%(segnum)d] is %(hash)s",
                         segnum=len(self._plaintext_segment_hashes)-1,
                         hash=base32.b2a(p.digest()),
                         level=log.NOISY)

            offset += this_segment


    def read_encrypted(self, length, hash_only):
        # make sure our parameters have been set up first
        d = self.get_all_encoding_parameters()
        # and size
        d.addCallback(lambda ignored: self.get_size())
        d.addCallback(lambda ignored: self._get_encryptor())
        # then fetch and encrypt the plaintext. The unusual structure here
        # (passing a Deferred *into* a function) is needed to avoid
        # overflowing the stack: Deferreds don't optimize out tail recursion.
        # We also pass in a list, to which _read_encrypted will append
        # ciphertext.
        ciphertext = []
        d2 = defer.Deferred()
        d.addCallback(lambda ignored:
                      self._read_encrypted(length, ciphertext, hash_only, d2))
        d.addCallback(lambda ignored: d2)
        return d

    def _read_encrypted(self, remaining, ciphertext, hash_only, fire_when_done):
        if not remaining:
            fire_when_done.callback(ciphertext)
            return None
        # tolerate large length= values without consuming a lot of RAM by
        # reading just a chunk (say 50kB) at a time. This only really matters
        # when hash_only==True (i.e. resuming an interrupted upload), since
        # that's the case where we will be skipping over a lot of data.
        size = min(remaining, self.CHUNKSIZE)
        remaining = remaining - size
        # read a chunk of plaintext..
        d = defer.maybeDeferred(self.original.read, size)
        # N.B.: if read() is synchronous, then since everything else is
        # actually synchronous too, we'd blow the stack unless we stall for a
        # tick. Once you accept a Deferred from IUploadable.read(), you must
        # be prepared to have it fire immediately too.
        d.addCallback(fireEventually)
        def _good(plaintext):
            # and encrypt it..
            # o/' over the fields we go, hashing all the way, sHA! sHA! sHA! o/'
            ct = self._hash_and_encrypt_plaintext(plaintext, hash_only)
            ciphertext.extend(ct)
            self._read_encrypted(remaining, ciphertext, hash_only,
                                 fire_when_done)
        def _err(why):
            fire_when_done.errback(why)
        d.addCallback(_good)
        d.addErrback(_err)
        return None

    def _hash_and_encrypt_plaintext(self, data, hash_only):
        assert isinstance(data, (tuple, list)), type(data)
        data = list(data)
        cryptdata = []
        # we use data.pop(0) instead of 'for chunk in data' to save
        # memory: each chunk is destroyed as soon as we're done with it.
        bytes_processed = 0
        while data:
            chunk = data.pop(0)
            self.log(" read_encrypted handling %dB-sized chunk" % len(chunk),
                     level=log.NOISY)
            bytes_processed += len(chunk)
            self._plaintext_hasher.update(chunk)
            self._update_segment_hash(chunk)
            # TODO: we have to encrypt the data (even if hash_only==True)
            # because pycryptopp's AES-CTR implementation doesn't offer a
            # way to change the counter value. Once pycryptopp acquires
            # this ability, change this to simply update the counter
            # before each call to (hash_only==False) _encryptor.process()
            ciphertext = self._encryptor.process(chunk)
            if hash_only:
                self.log("  skipping encryption", level=log.NOISY)
            else:
                cryptdata.append(ciphertext)
            del ciphertext
            del chunk
        self._ciphertext_bytes_read += bytes_processed
        if self._status:
            progress = float(self._ciphertext_bytes_read) / self._file_size
            self._status.set_progress(1, progress)
        return cryptdata

    def get_plaintext_hashtree_leaves(self, first, last, num_segments):
        """OBSOLETE; Get the leaf nodes of a merkle hash tree over the
        plaintext segments, i.e. get the tagged hashes of the given segments.
        The segment size is expected to be generated by the
        IEncryptedUploadable before any plaintext is read or ciphertext
        produced, so that the segment hashes can be generated with only a
        single pass.

        This returns a Deferred that fires with a sequence of hashes, using:

         tuple(segment_hashes[first:last])

        'num_segments' is used to assert that the number of segments that the
        IEncryptedUploadable handled matches the number of segments that the
        encoder was expecting.

        This method must not be called until the final byte has been read
        from read_encrypted(). Once this method is called, read_encrypted()
        can never be called again.
        """
        # this is currently unused, but will live again when we fix #453
        if len(self._plaintext_segment_hashes) < num_segments:
            # close out the last one
            assert len(self._plaintext_segment_hashes) == num_segments-1
            p, segment_left = self._get_segment_hasher()
            self._plaintext_segment_hashes.append(p.digest())
            del self._plaintext_segment_hasher
            self.log("closing plaintext leaf hasher, hashed %d bytes" %
                     self._plaintext_segment_hashed_bytes,
                     level=log.NOISY)
            self.log(format="plaintext leaf hash [%(segnum)d] is %(hash)s",
                     segnum=len(self._plaintext_segment_hashes)-1,
                     hash=base32.b2a(p.digest()),
                     level=log.NOISY)
        assert len(self._plaintext_segment_hashes) == num_segments
        return defer.succeed(tuple(self._plaintext_segment_hashes[first:last]))

    def get_plaintext_hash(self):
        """OBSOLETE; Get the hash of the whole plaintext.

        This returns a Deferred that fires with a tagged SHA-256 hash of the
        whole plaintext, obtained from hashutil.plaintext_hash(data).
        """
        # this is currently unused, but will live again when we fix #453
        h = self._plaintext_hasher.digest()
        return defer.succeed(h)

    def close(self):
        return self.original.close()

class UploadStatus:
    implements(IUploadStatus)
    statusid_counter = itertools.count(0)

    def __init__(self):
        self.storage_index = None
        self.size = None
        self.helper = False
        self.status = "Not started"
        self.progress = [0.0, 0.0, 0.0]
        self.active = True
        self.results = None
        self.counter = self.statusid_counter.next()
        self.started = time.time()

    def get_started(self):
        return self.started
    def get_storage_index(self):
        return self.storage_index
    def get_size(self):
        return self.size
    def using_helper(self):
        return self.helper
    def get_status(self):
        return self.status
    def get_progress(self):
        return tuple(self.progress)
    def get_active(self):
        return self.active
    def get_results(self):
        return self.results
    def get_counter(self):
        return self.counter

    def set_storage_index(self, si):
        self.storage_index = si
    def set_size(self, size):
        self.size = size
    def set_helper(self, helper):
        self.helper = helper
    def set_status(self, status):
        self.status = status
    def set_progress(self, which, value):
        # [0]: chk, [1]: ciphertext, [2]: encode+push
        self.progress[which] = value
    def set_active(self, value):
        self.active = value
    def set_results(self, value):
        self.results = value

class CHKUploader:
    server_selector_class = Tahoe2ServerSelector

    def __init__(self, storage_broker, secret_holder, progress=None):
        # server_selector needs storage_broker and secret_holder
        self._storage_broker = storage_broker
        self._secret_holder = secret_holder
        self._log_number = self.log("CHKUploader starting", parent=None)
        self._encoder = None
        self._storage_index = None
        self._upload_status = UploadStatus()
        self._upload_status.set_helper(False)
        self._upload_status.set_active(True)
        self._progress = progress

        # locate_all_shareholders() will create the following attribute:
        # self._server_trackers = {} # k: shnum, v: instance of ServerTracker

    def log(self, *args, **kwargs):
        if "parent" not in kwargs:
            kwargs["parent"] = self._log_number
        if "facility" not in kwargs:
            kwargs["facility"] = "tahoe.upload"
        return log.msg(*args, **kwargs)

    def start(self, encrypted_uploadable):
        """Start uploading the file.

        Returns a Deferred that will fire with the UploadResults instance.
        """

        self._started = time.time()
        eu = IEncryptedUploadable(encrypted_uploadable)
        self.log("starting upload of %s" % eu)

        eu.set_upload_status(self._upload_status)
        d = self.start_encrypted(eu)
        def _done(uploadresults):
            self._upload_status.set_active(False)
            return uploadresults
        d.addBoth(_done)
        return d

    def abort(self):
        """Call this if the upload must be abandoned before it completes.
        This will tell the shareholders to delete their partial shares. I
        return a Deferred that fires when these messages have been acked."""
        if not self._encoder:
            # how did you call abort() before calling start() ?
            return defer.succeed(None)
        return self._encoder.abort()

    def start_encrypted(self, encrypted):
        """ Returns a Deferred that will fire with the UploadResults instance. """
        eu = IEncryptedUploadable(encrypted)

        started = time.time()
        self._encoder = e = encode.Encoder(
            self._log_number,
            self._upload_status,
            progress=self._progress,
        )
        d = e.set_encrypted_uploadable(eu)
        d.addCallback(self.locate_all_shareholders, started)
        d.addCallback(self.set_shareholders, e)
        d.addCallback(lambda res: e.start())
        d.addCallback(self._encrypted_done)
        return d

    def locate_all_shareholders(self, encoder, started):
        server_selection_started = now = time.time()
        self._storage_index_elapsed = now - started
        storage_broker = self._storage_broker
        secret_holder = self._secret_holder
        storage_index = encoder.get_param("storage_index")
        self._storage_index = storage_index
        upload_id = si_b2a(storage_index)[:5]
        self.log("using storage index %s" % upload_id)
        server_selector = self.server_selector_class(upload_id,
                                                     self._log_number,
                                                     self._upload_status)

        share_size = encoder.get_param("share_size")
        block_size = encoder.get_param("block_size")
        num_segments = encoder.get_param("num_segments")
        k,desired,n = encoder.get_param("share_counts")

        self._server_selection_started = time.time()
        d = server_selector.get_shareholders(storage_broker, secret_holder,
                                             storage_index,
                                             share_size, block_size,
                                             num_segments, n, k, desired)
        def _done(res):
            self._server_selection_elapsed = time.time() - server_selection_started
            return res
        d.addCallback(_done)
        return d

    def set_shareholders(self, (upload_trackers, already_serverids), encoder):
        """
        @param upload_trackers: a sequence of ServerTracker objects that
                                have agreed to hold some shares for us (the
                                shareids are stashed inside the ServerTracker)

        @paran already_serverids: a dict mapping sharenum to a set of
                                  serverids for servers that claim to already
                                  have this share
        """
        msgtempl = "set_shareholders; upload_trackers is %s, already_serverids is %s"
        values = ([', '.join([str_shareloc(k,v)
                              for k,v in st.buckets.iteritems()])
                   for st in upload_trackers], already_serverids)
        self.log(msgtempl % values, level=log.OPERATIONAL)
        # record already-present shares in self._results
        self._count_preexisting_shares = len(already_serverids)

        self._server_trackers = {} # k: shnum, v: instance of ServerTracker
        for tracker in upload_trackers:
            assert isinstance(tracker, ServerTracker)
        buckets = {}
        servermap = already_serverids.copy()
        for tracker in upload_trackers:
            buckets.update(tracker.buckets)
            for shnum in tracker.buckets:
                self._server_trackers[shnum] = tracker
                servermap.setdefault(shnum, set()).add(tracker.get_serverid())
        assert len(buckets) == sum([len(tracker.buckets)
                                    for tracker in upload_trackers]), \
            "%s (%s) != %s (%s)" % (
                len(buckets),
                buckets,
                sum([len(tracker.buckets) for tracker in upload_trackers]),
                [(t.buckets, t.get_serverid()) for t in upload_trackers]
                )
        encoder.set_shareholders(buckets, servermap)

    def _encrypted_done(self, verifycap):
        """Returns a Deferred that will fire with the UploadResults instance."""
        e = self._encoder
        sharemap = dictutil.DictOfSets()
        servermap = dictutil.DictOfSets()
        for shnum in e.get_shares_placed():
            server = self._server_trackers[shnum].get_server()
            sharemap.add(shnum, server)
            servermap.add(server, shnum)
        now = time.time()
        timings = {}
        timings["total"] = now - self._started
        timings["storage_index"] = self._storage_index_elapsed
        timings["peer_selection"] = self._server_selection_elapsed
        timings.update(e.get_times())
        ur = UploadResults(file_size=e.file_size,
                           ciphertext_fetched=0,
                           preexisting_shares=self._count_preexisting_shares,
                           pushed_shares=len(e.get_shares_placed()),
                           sharemap=sharemap,
                           servermap=servermap,
                           timings=timings,
                           uri_extension_data=e.get_uri_extension_data(),
                           uri_extension_hash=e.get_uri_extension_hash(),
                           verifycapstr=verifycap.to_string())
        self._upload_status.set_results(ur)
        return ur

    def get_upload_status(self):
        return self._upload_status

def read_this_many_bytes(uploadable, size, prepend_data=[]):
    if size == 0:
        return defer.succeed([])
    d = uploadable.read(size)
    def _got(data):
        assert isinstance(data, list)
        bytes = sum([len(piece) for piece in data])
        assert bytes > 0
        assert bytes <= size
        remaining = size - bytes
        if remaining:
            return read_this_many_bytes(uploadable, remaining,
                                        prepend_data + data)
        return prepend_data + data
    d.addCallback(_got)
    return d

class LiteralUploader:

    def __init__(self, progress=None):
        self._status = s = UploadStatus()
        s.set_storage_index(None)
        s.set_helper(False)
        s.set_progress(0, 1.0)
        s.set_active(False)
        self._progress = progress

    def start(self, uploadable):
        uploadable = IUploadable(uploadable)
        d = uploadable.get_size()
        def _got_size(size):
            self._size = size
            self._status.set_size(size)
            if self._progress:
                self._progress.set_progress_total(size)
            return read_this_many_bytes(uploadable, size)
        d.addCallback(_got_size)
        d.addCallback(lambda data: uri.LiteralFileURI("".join(data)))
        d.addCallback(lambda u: u.to_string())
        d.addCallback(self._build_results)
        return d

    def _build_results(self, uri):
        ur = UploadResults(file_size=self._size,
                           ciphertext_fetched=0,
                           preexisting_shares=0,
                           pushed_shares=0,
                           sharemap={},
                           servermap={},
                           timings={},
                           uri_extension_data=None,
                           uri_extension_hash=None,
                           verifycapstr=None)
        ur.set_uri(uri)
        self._status.set_status("Finished")
        self._status.set_progress(1, 1.0)
        self._status.set_progress(2, 1.0)
        self._status.set_results(ur)
        if self._progress:
            self._progress.set_progress(self._size)
        return ur

    def close(self):
        pass

    def get_upload_status(self):
        return self._status

class RemoteEncryptedUploadable(Referenceable):
    implements(RIEncryptedUploadable)

    def __init__(self, encrypted_uploadable, upload_status):
        self._eu = IEncryptedUploadable(encrypted_uploadable)
        self._offset = 0
        self._bytes_sent = 0
        self._status = IUploadStatus(upload_status)
        # we are responsible for updating the status string while we run, and
        # for setting the ciphertext-fetch progress.
        self._size = None

    def get_size(self):
        if self._size is not None:
            return defer.succeed(self._size)
        d = self._eu.get_size()
        def _got_size(size):
            self._size = size
            return size
        d.addCallback(_got_size)
        return d

    def remote_get_size(self):
        return self.get_size()
    def remote_get_all_encoding_parameters(self):
        return self._eu.get_all_encoding_parameters()

    def _read_encrypted(self, length, hash_only):
        d = self._eu.read_encrypted(length, hash_only)
        def _read(strings):
            if hash_only:
                self._offset += length
            else:
                size = sum([len(data) for data in strings])
                self._offset += size
            return strings
        d.addCallback(_read)
        return d

    def remote_read_encrypted(self, offset, length):
        # we don't support seek backwards, but we allow skipping forwards
        precondition(offset >= 0, offset)
        precondition(length >= 0, length)
        lp = log.msg("remote_read_encrypted(%d-%d)" % (offset, offset+length),
                     level=log.NOISY)
        precondition(offset >= self._offset, offset, self._offset)
        if offset > self._offset:
            # read the data from disk anyways, to build up the hash tree
            skip = offset - self._offset
            log.msg("remote_read_encrypted skipping ahead from %d to %d, skip=%d" %
                    (self._offset, offset, skip), level=log.UNUSUAL, parent=lp)
            d = self._read_encrypted(skip, hash_only=True)
        else:
            d = defer.succeed(None)

        def _at_correct_offset(res):
            assert offset == self._offset, "%d != %d" % (offset, self._offset)
            return self._read_encrypted(length, hash_only=False)
        d.addCallback(_at_correct_offset)

        def _read(strings):
            size = sum([len(data) for data in strings])
            self._bytes_sent += size
            return strings
        d.addCallback(_read)
        return d

    def remote_close(self):
        return self._eu.close()


class AssistedUploader:

    def __init__(self, helper, storage_broker):
        self._helper = helper
        self._storage_broker = storage_broker
        self._log_number = log.msg("AssistedUploader starting")
        self._storage_index = None
        self._upload_status = s = UploadStatus()
        s.set_helper(True)
        s.set_active(True)

    def log(self, *args, **kwargs):
        if "parent" not in kwargs:
            kwargs["parent"] = self._log_number
        return log.msg(*args, **kwargs)

    def start(self, encrypted_uploadable, storage_index):
        """Start uploading the file.

        Returns a Deferred that will fire with the UploadResults instance.
        """
        precondition(isinstance(storage_index, str), storage_index)
        self._started = time.time()
        eu = IEncryptedUploadable(encrypted_uploadable)
        eu.set_upload_status(self._upload_status)
        self._encuploadable = eu
        self._storage_index = storage_index
        d = eu.get_size()
        d.addCallback(self._got_size)
        d.addCallback(lambda res: eu.get_all_encoding_parameters())
        d.addCallback(self._got_all_encoding_parameters)
        d.addCallback(self._contact_helper)
        d.addCallback(self._build_verifycap)
        def _done(res):
            self._upload_status.set_active(False)
            return res
        d.addBoth(_done)
        return d

    def _got_size(self, size):
        self._size = size
        self._upload_status.set_size(size)

    def _got_all_encoding_parameters(self, params):
        k, happy, n, segment_size = params
        # stash these for URI generation later
        self._needed_shares = k
        self._total_shares = n
        self._segment_size = segment_size

    def _contact_helper(self, res):
        now = self._time_contacting_helper_start = time.time()
        self._storage_index_elapsed = now - self._started
        self.log(format="contacting helper for SI %(si)s..",
                 si=si_b2a(self._storage_index), level=log.NOISY)
        self._upload_status.set_status("Contacting Helper")
        d = self._helper.callRemote("upload_chk", self._storage_index)
        d.addCallback(self._contacted_helper)
        return d

    def _contacted_helper(self, (helper_upload_results, upload_helper)):
        now = time.time()
        elapsed = now - self._time_contacting_helper_start
        self._elapsed_time_contacting_helper = elapsed
        if upload_helper:
            self.log("helper says we need to upload", level=log.NOISY)
            self._upload_status.set_status("Uploading Ciphertext")
            # we need to upload the file
            reu = RemoteEncryptedUploadable(self._encuploadable,
                                            self._upload_status)
            # let it pre-compute the size for progress purposes
            d = reu.get_size()
            d.addCallback(lambda ignored:
                          upload_helper.callRemote("upload", reu))
            # this Deferred will fire with the upload results
            return d
        self.log("helper says file is already uploaded", level=log.OPERATIONAL)
        self._upload_status.set_progress(1, 1.0)
        return helper_upload_results

    def _convert_old_upload_results(self, upload_results):
        # pre-1.3.0 helpers return upload results which contain a mapping
        # from shnum to a single human-readable string, containing things
        # like "Found on [x],[y],[z]" (for healthy files that were already in
        # the grid), "Found on [x]" (for files that needed upload but which
        # discovered pre-existing shares), and "Placed on [x]" (for newly
        # uploaded shares). The 1.3.0 helper returns a mapping from shnum to
        # set of binary serverid strings.

        # the old results are too hard to deal with (they don't even contain
        # as much information as the new results, since the nodeids are
        # abbreviated), so if we detect old results, just clobber them.

        sharemap = upload_results.sharemap
        if str in [type(v) for v in sharemap.values()]:
            upload_results.sharemap = None

    def _build_verifycap(self, helper_upload_results):
        self.log("upload finished, building readcap", level=log.OPERATIONAL)
        self._convert_old_upload_results(helper_upload_results)
        self._upload_status.set_status("Building Readcap")
        hur = helper_upload_results
        assert hur.uri_extension_data["needed_shares"] == self._needed_shares
        assert hur.uri_extension_data["total_shares"] == self._total_shares
        assert hur.uri_extension_data["segment_size"] == self._segment_size
        assert hur.uri_extension_data["size"] == self._size

        # hur.verifycap doesn't exist if already found
        v = uri.CHKFileVerifierURI(self._storage_index,
                                   uri_extension_hash=hur.uri_extension_hash,
                                   needed_shares=self._needed_shares,
                                   total_shares=self._total_shares,
                                   size=self._size)
        timings = {}
        timings["storage_index"] = self._storage_index_elapsed
        timings["contacting_helper"] = self._elapsed_time_contacting_helper
        for key,val in hur.timings.items():
            if key == "total":
                key = "helper_total"
            timings[key] = val
        now = time.time()
        timings["total"] = now - self._started

        gss = self._storage_broker.get_stub_server
        sharemap = {}
        servermap = {}
        for shnum, serverids in hur.sharemap.items():
            sharemap[shnum] = set([gss(serverid) for serverid in serverids])
        # if the file was already in the grid, hur.servermap is an empty dict
        for serverid, shnums in hur.servermap.items():
            servermap[gss(serverid)] = set(shnums)

        ur = UploadResults(file_size=self._size,
                           # not if already found
                           ciphertext_fetched=hur.ciphertext_fetched,
                           preexisting_shares=hur.preexisting_shares,
                           pushed_shares=hur.pushed_shares,
                           sharemap=sharemap,
                           servermap=servermap,
                           timings=timings,
                           uri_extension_data=hur.uri_extension_data,
                           uri_extension_hash=hur.uri_extension_hash,
                           verifycapstr=v.to_string())

        self._upload_status.set_status("Finished")
        self._upload_status.set_results(ur)
        return ur

    def get_upload_status(self):
        return self._upload_status

class BaseUploadable:
    # this is overridden by max_segment_size
    default_max_segment_size = DEFAULT_MAX_SEGMENT_SIZE
    default_params_set = False

    max_segment_size = None
    encoding_param_k = None
    encoding_param_happy = None
    encoding_param_n = None

    _all_encoding_parameters = None
    _status = None

    def set_upload_status(self, upload_status):
        self._status = IUploadStatus(upload_status)

    def set_default_encoding_parameters(self, default_params):
        assert isinstance(default_params, dict)
        for k,v in default_params.items():
            precondition(isinstance(k, str), k, v)
            precondition(isinstance(v, int), k, v)
        if "k" in default_params:
            self.default_encoding_param_k = default_params["k"]
        if "happy" in default_params:
            self.default_encoding_param_happy = default_params["happy"]
        if "n" in default_params:
            self.default_encoding_param_n = default_params["n"]
        if "max_segment_size" in default_params:
            self.default_max_segment_size = default_params["max_segment_size"]
        self.default_params_set = True

    def get_all_encoding_parameters(self):
        _assert(self.default_params_set, "set_default_encoding_parameters not called on %r" % (self,))
        if self._all_encoding_parameters:
            return defer.succeed(self._all_encoding_parameters)

        max_segsize = self.max_segment_size or self.default_max_segment_size
        k = self.encoding_param_k or self.default_encoding_param_k
        happy = self.encoding_param_happy or self.default_encoding_param_happy
        n = self.encoding_param_n or self.default_encoding_param_n

        d = self.get_size()
        def _got_size(file_size):
            # for small files, shrink the segment size to avoid wasting space
            segsize = min(max_segsize, file_size)
            # this must be a multiple of 'required_shares'==k
            segsize = mathutil.next_multiple(segsize, k)
            encoding_parameters = (k, happy, n, segsize)
            self._all_encoding_parameters = encoding_parameters
            return encoding_parameters
        d.addCallback(_got_size)
        return d

class FileHandle(BaseUploadable):
    implements(IUploadable)

    def __init__(self, filehandle, convergence):
        """
        Upload the data from the filehandle.  If convergence is None then a
        random encryption key will be used, else the plaintext will be hashed,
        then the hash will be hashed together with the string in the
        "convergence" argument to form the encryption key.
        """
        assert convergence is None or isinstance(convergence, str), (convergence, type(convergence))
        self._filehandle = filehandle
        self._key = None
        self.convergence = convergence
        self._size = None

    def _get_encryption_key_convergent(self):
        if self._key is not None:
            return defer.succeed(self._key)

        d = self.get_size()
        # that sets self._size as a side-effect
        d.addCallback(lambda size: self.get_all_encoding_parameters())
        def _got(params):
            k, happy, n, segsize = params
            f = self._filehandle
            enckey_hasher = convergence_hasher(k, n, segsize, self.convergence)
            f.seek(0)
            BLOCKSIZE = 64*1024
            bytes_read = 0
            while True:
                data = f.read(BLOCKSIZE)
                if not data:
                    break
                enckey_hasher.update(data)
                # TODO: setting progress in a non-yielding loop is kind of
                # pointless, but I'm anticipating (perhaps prematurely) the
                # day when we use a slowjob or twisted's CooperatorService to
                # make this yield time to other jobs.
                bytes_read += len(data)
                if self._status:
                    self._status.set_progress(0, float(bytes_read)/self._size)
            f.seek(0)
            self._key = enckey_hasher.digest()
            if self._status:
                self._status.set_progress(0, 1.0)
            assert len(self._key) == 16
            return self._key
        d.addCallback(_got)
        return d

    def _get_encryption_key_random(self):
        if self._key is None:
            self._key = os.urandom(16)
        return defer.succeed(self._key)

    def get_encryption_key(self):
        if self.convergence is not None:
            return self._get_encryption_key_convergent()
        else:
            return self._get_encryption_key_random()

    def get_size(self):
        if self._size is not None:
            return defer.succeed(self._size)
        self._filehandle.seek(0, os.SEEK_END)
        size = self._filehandle.tell()
        self._size = size
        self._filehandle.seek(0)
        return defer.succeed(size)

    def read(self, length):
        return defer.succeed([self._filehandle.read(length)])

    def close(self):
        # the originator of the filehandle reserves the right to close it
        pass

class FileName(FileHandle):
    def __init__(self, filename, convergence):
        """
        Upload the data from the filename.  If convergence is None then a
        random encryption key will be used, else the plaintext will be hashed,
        then the hash will be hashed together with the string in the
        "convergence" argument to form the encryption key.
        """
        assert convergence is None or isinstance(convergence, str), (convergence, type(convergence))
        FileHandle.__init__(self, open(filename, "rb"), convergence=convergence)
    def close(self):
        FileHandle.close(self)
        self._filehandle.close()

class Data(FileHandle):
    def __init__(self, data, convergence):
        """
        Upload the data from the data argument.  If convergence is None then a
        random encryption key will be used, else the plaintext will be hashed,
        then the hash will be hashed together with the string in the
        "convergence" argument to form the encryption key.
        """
        assert convergence is None or isinstance(convergence, str), (convergence, type(convergence))
        FileHandle.__init__(self, StringIO(data), convergence=convergence)

class Uploader(service.MultiService, log.PrefixingLogMixin):
    """I am a service that allows file uploading. I am a service-child of the
    Client.
    """
    implements(IUploader)
    name = "uploader"
    URI_LIT_SIZE_THRESHOLD = 55

    def __init__(self, helper_furl=None, stats_provider=None, history=None, progress=None):
        self._helper_furl = helper_furl
        self.stats_provider = stats_provider
        self._history = history
        self._helper = None
        self._all_uploads = weakref.WeakKeyDictionary() # for debugging
        self._progress = progress
        log.PrefixingLogMixin.__init__(self, facility="tahoe.immutable.upload")
        service.MultiService.__init__(self)

    def startService(self):
        service.MultiService.startService(self)
        if self._helper_furl:
            self.parent.tub.connectTo(self._helper_furl,
                                      self._got_helper)

    def _got_helper(self, helper):
        self.log("got helper connection, getting versions")
        default = { "http://allmydata.org/tahoe/protocols/helper/v1" :
                    { },
                    "application-version": "unknown: no get_version()",
                    }
        d = add_version_to_remote_reference(helper, default)
        d.addCallback(self._got_versioned_helper)

    def _got_versioned_helper(self, helper):
        needed = "http://allmydata.org/tahoe/protocols/helper/v1"
        if needed not in helper.version:
            raise InsufficientVersionError(needed, helper.version)
        self._helper = helper
        helper.notifyOnDisconnect(self._lost_helper)

    def _lost_helper(self):
        self._helper = None

    def get_helper_info(self):
        # return a tuple of (helper_furl_or_None, connected_bool)
        return (self._helper_furl, bool(self._helper))


    def upload(self, uploadable, progress=None):
        """
        Returns a Deferred that will fire with the UploadResults instance.
        """
        assert self.parent
        assert self.running
        assert progress is None or IProgress.providedBy(progress)

        uploadable = IUploadable(uploadable)
        d = uploadable.get_size()
        def _got_size(size):
            default_params = self.parent.get_encoding_parameters()
            precondition(isinstance(default_params, dict), default_params)
            precondition("max_segment_size" in default_params, default_params)
            uploadable.set_default_encoding_parameters(default_params)
            if progress:
                progress.set_progress_total(size)

            if self.stats_provider:
                self.stats_provider.count('uploader.files_uploaded', 1)
                self.stats_provider.count('uploader.bytes_uploaded', size)

            if size <= self.URI_LIT_SIZE_THRESHOLD:
                uploader = LiteralUploader(progress=progress)
                return uploader.start(uploadable)
            else:
                eu = EncryptAnUploadable(uploadable, self._parentmsgid)
                d2 = defer.succeed(None)
                storage_broker = self.parent.get_storage_broker()
                if self._helper:
                    uploader = AssistedUploader(self._helper, storage_broker)
                    d2.addCallback(lambda x: eu.get_storage_index())
                    d2.addCallback(lambda si: uploader.start(eu, si))
                else:
                    storage_broker = self.parent.get_storage_broker()
                    secret_holder = self.parent._secret_holder
                    uploader = CHKUploader(storage_broker, secret_holder, progress=progress)
                    d2.addCallback(lambda x: uploader.start(eu))

                self._all_uploads[uploader] = None
                if self._history:
                    self._history.add_upload(uploader.get_upload_status())
                def turn_verifycap_into_read_cap(uploadresults):
                    # Generate the uri from the verifycap plus the key.
                    d3 = uploadable.get_encryption_key()
                    def put_readcap_into_results(key):
                        v = uri.from_string(uploadresults.get_verifycapstr())
                        r = uri.CHKFileURI(key, v.uri_extension_hash, v.needed_shares, v.total_shares, v.size)
                        uploadresults.set_uri(r.to_string())
                        return uploadresults
                    d3.addCallback(put_readcap_into_results)
                    return d3
                d2.addCallback(turn_verifycap_into_read_cap)
                return d2
        d.addCallback(_got_size)
        def _done(res):
            uploadable.close()
            return res
        d.addBoth(_done)
        return d
