import os, stat, time, weakref
from allmydata import node
from base64 import urlsafe_b64encode

from zope.interface import implements
from twisted.internet import reactor, defer
from twisted.application import service
from twisted.application.internet import TimerService
from pycryptopp.publickey import rsa

import allmydata
from allmydata.node import InvalidValueError
from allmydata.storage.server import StorageServer
from allmydata.storage.backends.null.null_backend import configure_null_backend
from allmydata.storage.backends.disk.disk_backend import configure_disk_backend
from allmydata.storage.backends.cloud.cloud_backend import configure_cloud_backend
from allmydata.storage.backends.cloud.mock_cloud import configure_mock_cloud_backend
from allmydata.storage.expiration import ExpirationPolicy
from allmydata import storage_client
from allmydata.immutable.upload import Uploader
from allmydata.immutable.offloaded import Helper
from allmydata.control import ControlServer
from allmydata.introducer.client import IntroducerClient
from allmydata.util import hashutil, base32, pollmixin, log, keyutil, idlib
from allmydata.util.encodingutil import get_filesystem_encoding, quote_output, \
     from_utf8_or_none
from allmydata.util.fileutil import abspath_expanduser_unicode
from allmydata.util.time_format import parse_duration, parse_date
from allmydata.stats import StatsProvider
from allmydata.history import History
from allmydata.interfaces import IStatsProducer, SDMF_VERSION, MDMF_VERSION
from allmydata.nodemaker import NodeMaker
from allmydata.blacklist import Blacklist
from allmydata.node import OldConfigOptionError


KiB=1024
MiB=1024*KiB
GiB=1024*MiB
TiB=1024*GiB
PiB=1024*TiB

def _make_secret():
    return base32.b2a(os.urandom(hashutil.CRYPTO_VAL_SIZE)) + "\n"

class SecretHolder:
    def __init__(self, lease_secret, convergence_secret):
        self._lease_secret = lease_secret
        self._convergence_secret = convergence_secret

    def get_renewal_secret(self):
        return hashutil.my_renewal_secret_hash(self._lease_secret)

    def get_cancel_secret(self):
        return hashutil.my_cancel_secret_hash(self._lease_secret)

    def get_convergence_secret(self):
        return self._convergence_secret

class KeyGenerator:
    """I create RSA keys for mutable files. Each call to generate() returns a
    single keypair. The keysize is specified first by the keysize= argument
    to generate(), then with a default set by set_default_keysize(), then
    with a built-in default of 2048 bits."""
    def __init__(self):
        self._remote = None
        self.default_keysize = 2048

    def set_remote_generator(self, keygen):
        self._remote = keygen
    def set_default_keysize(self, keysize):
        """Call this to override the size of the RSA keys created for new
        mutable files which don't otherwise specify a size. This will affect
        all subsequent calls to generate() without a keysize= argument. The
        default size is 2048 bits. Test cases should call this method once
        during setup, to cause me to create smaller keys, so the unit tests
        run faster."""
        self.default_keysize = keysize

    def generate(self, keysize=None):
        """I return a Deferred that fires with a (verifyingkey, signingkey)
        pair. I accept a keysize in bits (2048 bit keys are standard, smaller
        keys are used for testing). If you do not provide a keysize, I will
        use my default, which is set by a call to set_default_keysize(). If
        set_default_keysize() has never been called, I will create 2048 bit
        keys."""
        keysize = keysize or self.default_keysize
        if self._remote:
            d = self._remote.callRemote('get_rsa_key_pair', keysize)
            def make_key_objs((verifying_key, signing_key)):
                v = rsa.create_verifying_key_from_string(verifying_key)
                s = rsa.create_signing_key_from_string(signing_key)
                return v, s
            d.addCallback(make_key_objs)
            return d
        else:
            # RSA key generation for a 2048 bit key takes between 0.8 and 3.2
            # secs
            signer = rsa.generate(keysize)
            verifier = signer.get_verifying_key()
            return defer.succeed( (verifier, signer) )

class Terminator(service.Service):
    def __init__(self):
        self._clients = weakref.WeakKeyDictionary()
    def register(self, c):
        self._clients[c] = None
    def stopService(self):
        for c in self._clients:
            c.stop()
        return service.Service.stopService(self)


class Client(node.Node, pollmixin.PollMixin):
    implements(IStatsProducer)

    PORTNUMFILE = "client.port"
    STOREDIR = 'storage'
    NODETYPE = "client"
    EXIT_TRIGGER_FILE = "exit_trigger"

    # This means that if a storage server treats me as though I were a
    # 1.0.0 storage client, it will work as they expect.
    OLDEST_SUPPORTED_VERSION = "1.0.0"

    # This is a dictionary of (needed, desired, total, max_segment_size). 'needed'
    # is the number of shares required to reconstruct a file. 'desired' means
    # that we will abort an upload unless we can allocate space for at least
    # this many. 'total' is the total number of shares created by encoding.
    # If everybody has room then this is is how many we will upload.
    DEFAULT_ENCODING_PARAMETERS = {"k": 3,
                                   "happy": 7,
                                   "n": 10,
                                   "max_segment_size": 128*KiB,
                                   }

    def __init__(self, basedir="."):
        node.Node.__init__(self, basedir)
        self.started_timestamp = time.time()
        self.logSource="Client"
        self.encoding_params = self.DEFAULT_ENCODING_PARAMETERS.copy()
        self.init_introducer_client()
        self.init_stats_provider()
        self.init_secrets()
        self.init_node_key()
        self.init_storage()
        self.init_control()
        self.helper = None
        if self.get_config("helper", "enabled", False, boolean=True):
            self.init_helper()
        self._key_generator = KeyGenerator()
        key_gen_furl = self.get_config("client", "key_generator.furl", None)
        if key_gen_furl:
            self.init_key_gen(key_gen_furl)
        self.init_client()
        # ControlServer and Helper are attached after Tub startup
        self.init_ftp_server()
        self.init_sftp_server()
        self.init_drop_uploader()

        # If the node sees an exit_trigger file, it will poll every second to see
        # whether the file still exists, and what its mtime is. If the file does not
        # exist or has not been modified for a given timeout, the node will exit.
        exit_trigger_file = os.path.join(self.basedir,
                                         self.EXIT_TRIGGER_FILE)
        if os.path.exists(exit_trigger_file):
            age = time.time() - os.stat(exit_trigger_file)[stat.ST_MTIME]
            self.log("%s file noticed (%ds old), starting timer" % (self.EXIT_TRIGGER_FILE, age))
            exit_trigger = TimerService(1.0, self._check_exit_trigger, exit_trigger_file)
            exit_trigger.setServiceParent(self)

        # this needs to happen last, so it can use getServiceNamed() to
        # acquire references to StorageServer and other web-statusable things
        webport = self.get_config("node", "web.port", None)
        if webport:
            self.init_web(webport) # strports string

    def _sequencer(self):
        seqnum_path = os.path.join(self.basedir, "announcement-seqnum")
        seqnum_s = self.get_optional_config_from_file(seqnum_path)
        if not seqnum_s:
            seqnum_s = "0"
        seqnum = int(seqnum_s.strip())
        seqnum += 1 # increment
        self.write_config("announcement-seqnum", "%d\n" % seqnum)
        nonce = _make_secret().strip()
        return seqnum, nonce

    def init_introducer_client(self):
        self.introducer_furl = self.get_config("client", "introducer.furl")
        ic = IntroducerClient(self.tub, self.introducer_furl,
                              self.nickname,
                              str(allmydata.__full_version__),
                              str(self.OLDEST_SUPPORTED_VERSION),
                              self.get_app_versions(),
                              self._sequencer)
        self.introducer_client = ic
        # hold off on starting the IntroducerClient until our tub has been
        # started, so we'll have a useful address on our RemoteReference, so
        # that the introducer's status page will show us.
        d = self.when_tub_ready()
        def _start_introducer_client(res):
            ic.setServiceParent(self)
        d.addCallback(_start_introducer_client)
        d.addErrback(log.err, facility="tahoe.init",
                     level=log.BAD, umid="URyI5w")

    def init_stats_provider(self):
        gatherer_furl = self.get_config("client", "stats_gatherer.furl", None)
        self.stats_provider = StatsProvider(self, gatherer_furl)
        self.add_service(self.stats_provider)
        self.stats_provider.register_producer(self)

    def get_stats(self):
        return { 'node.uptime': time.time() - self.started_timestamp }

    def init_secrets(self):
        lease_s = self.get_or_create_private_config("secret", _make_secret)
        lease_secret = base32.a2b(lease_s)
        convergence_s = self.get_or_create_private_config('convergence',
                                                          _make_secret)
        self.convergence = base32.a2b(convergence_s)
        self._secret_holder = SecretHolder(lease_secret, self.convergence)

    def init_node_key(self):
        # we only create the key once. On all subsequent runs, we re-use the
        # existing key
        def _make_key():
            sk_vs,vk_vs = keyutil.make_keypair()
            return sk_vs+"\n"

        sk_vs = self.get_or_create_private_config("node.privkey", _make_key)
        sk,vk_vs = keyutil.parse_privkey(sk_vs.strip())
        self.write_config("node.pubkey", vk_vs+"\n")
        self._node_key = sk

    def get_long_nodeid(self):
        # this matches what IServer.get_longname() says about us elsewhere
        vk_bytes = self._node_key.get_verifying_key_bytes()
        return "v0-"+base32.b2a(vk_bytes)

    def get_long_tubid(self):
        return idlib.nodeid_b2a(self.nodeid)

    def _init_permutation_seed(self, ss):
        seed = self.get_optional_private_config("permutation-seed")
        if not seed:
            if ss.backend.must_use_tubid_as_permutation_seed():
                # If a server using a disk backend has shares but not a recorded
                # permutation-seed, then it has been around since pre-#466
                # days, and the clients who uploaded those shares used our
                # TubID as a permutation-seed. We should keep using that same
                # seed to keep the shares in the same place in the permuted
                # ring, so those clients don't have to perform excessive
                # searches.
                seed = base32.b2a(self.nodeid)
            else:
                # otherwise, we're free to use the more natural seed of our
                # pubkey-based serverid
                vk_bytes = self._node_key.get_verifying_key_bytes()
                seed = base32.b2a(vk_bytes)
            self.write_config("permutation-seed", seed+"\n")
        return seed.strip()

    @classmethod
    def configure_backend(cls, config):
        """This is also called directly by the implementation of 'tahoe admin create-container'."""

        storedir = os.path.join(config.basedir, cls.STOREDIR)

        # What sort of backend?
        backendtype = config.get_config("storage", "backend", "disk")
        if backendtype == "s3":
            backendtype = "cloud.s3"
        backendprefix = backendtype.partition('.')[0]

        backend_configurators = {
            'disk': configure_disk_backend,
            'cloud': configure_cloud_backend,
            'mock_cloud': configure_mock_cloud_backend,
            'debug_discard': configure_null_backend,
        }

        if backendprefix not in backend_configurators:
            raise InvalidValueError("%s is not supported; it must start with one of %s"
                                    % (quote_output("[storage]backend = " + backendtype),
                                    backend_configurators.keys()) )

        return (backend_configurators[backendprefix](storedir, config), storedir)

    def init_storage(self):
        self.accountant = None
        # Should we run a storage server (and publish it for others to use)?
        if not self.get_config("storage", "enabled", True, boolean=True):
            return

        (backend, storedir) = self.configure_backend(self)

        if self.get_config("storage", "debug_discard", False, boolean=True):
            raise OldConfigOptionError("[storage]debug_discard = True is no longer supported.")

        expire = self.get_config("storage", "expire.enabled", False, boolean=True)
        if expire:
            mode = self.get_config("storage", "expire.mode") # require a mode
        else:
            mode = self.get_config("storage", "expire.mode", "age")

        o_l_d = self.get_config("storage", "expire.override_lease_duration", None)
        if o_l_d is not None:
            o_l_d = parse_duration(o_l_d)

        cutoff_date = None
        if mode == "cutoff-date":
            cutoff_date = self.get_config("storage", "expire.cutoff_date")
            cutoff_date = parse_date(cutoff_date)

        if not self.get_config("storage", "expire.immutable", True, boolean=True):
            raise OldConfigOptionError("[storage]expire.immutable = False is no longer supported.")
        if not self.get_config("storage", "expire.mutable", True, boolean=True):
            raise OldConfigOptionError("[storage]expire.mutable = False is no longer supported.")

        expiration_policy = ExpirationPolicy(enabled=expire, mode=mode, override_lease_duration=o_l_d,
                                             cutoff_date=cutoff_date)

        statedir = storedir
        ss = StorageServer(self.nodeid, backend, statedir,
                           stats_provider=self.stats_provider)
        self.accountant = ss.get_accountant()
        self.accountant.set_expiration_policy(expiration_policy)
        self.storage_server = ss
        self.add_service(ss)

        d = self.when_tub_ready()
        # we can't do registerReference until the Tub is ready
        def _publish(res):
            anonymous_account = self.accountant.get_anonymous_account()
            anonymous_account_furlfile = os.path.join(self.basedir, "private", "storage.furl").encode(get_filesystem_encoding())
            anonymous_account_furl = self.tub.registerReference(anonymous_account, furlFile=anonymous_account_furlfile)
            ann = {"anonymous-storage-FURL": anonymous_account_furl,
                   "permutation-seed-base32": self._init_permutation_seed(ss),
                   }
            self.introducer_client.publish("storage", ann, self._node_key)
        d.addCallback(_publish)
        d.addErrback(log.err, facility="tahoe.init",
                     level=log.BAD, umid="aLGBKw")

    def get_accountant(self):
        return self.accountant

    def init_client(self):
        helper_furl = self.get_config("client", "helper.furl", None)
        if helper_furl in ("None", ""):
            helper_furl = None

        DEP = self.encoding_params
        DEP["k"] = int(self.get_config("client", "shares.needed", DEP["k"]))
        DEP["n"] = int(self.get_config("client", "shares.total", DEP["n"]))
        DEP["happy"] = int(self.get_config("client", "shares.happy", DEP["happy"]))

        # for the CLI to authenticate to local JSON endpoints
        self._create_auth_token()

        self.init_client_storage_broker()
        self.history = History(self.stats_provider)
        self.terminator = Terminator()
        self.terminator.setServiceParent(self)
        self.add_service(Uploader(helper_furl, self.stats_provider,
                                  self.history))
        self.init_blacklist()
        self.init_nodemaker()

    def get_auth_token(self):
        """
        This returns a local authentication token, which is just some
        random data in "api_auth_token" which must be echoed to API
        calls.

        Currently only the URI '/magic' for magic-folder status; other
        endpoints are invited to include this as well, as appropriate.
        """
        return self.get_private_config('api_auth_token')

    def _create_auth_token(self):
        """
        Creates new auth-token data written to 'private/api_auth_token'.

        This is intentionally re-created every time the node starts.
        """
        self.write_private_config(
            'api_auth_token',
            urlsafe_b64encode(os.urandom(32)) + '\n',
        )

    def init_client_storage_broker(self):
        # create a StorageFarmBroker object, for use by Uploader/Downloader
        # (and everybody else who wants to use storage servers)
        ps = self.get_config("client", "peers.preferred", "").split(",")
        preferred_peers = tuple([p.strip() for p in ps if p != ""])
        sb = storage_client.StorageFarmBroker(self.tub, permute_peers=True, preferred_peers=preferred_peers)
        self.storage_broker = sb

        # load static server specifications from tahoe.cfg, if any.
        # Not quite ready yet.
        #if self.config.has_section("client-server-selection"):
        #    server_params = {} # maps serverid to dict of parameters
        #    for (name, value) in self.config.items("client-server-selection"):
        #        pieces = name.split(".")
        #        if pieces[0] == "server":
        #            serverid = pieces[1]
        #            if serverid not in server_params:
        #                server_params[serverid] = {}
        #            server_params[serverid][pieces[2]] = value
        #    for serverid, params in server_params.items():
        #        server_type = params.pop("type")
        #        if server_type == "tahoe-foolscap":
        #            s = storage_client.NativeStorageClient(*params)
        #        else:
        #            msg = ("unrecognized server type '%s' in "
        #                   "tahoe.cfg [client-server-selection]server.%s.type"
        #                   % (server_type, serverid))
        #            raise storage_client.UnknownServerTypeError(msg)
        #        sb.add_server(s.serverid, s)

        # check to see if we're supposed to use the introducer too
        if self.get_config("client-server-selection", "use_introducer",
                           default=True, boolean=True):
            sb.use_introducer(self.introducer_client)

    def get_storage_broker(self):
        return self.storage_broker

    def init_blacklist(self):
        fn = os.path.join(self.basedir, "access.blacklist")
        self.blacklist = Blacklist(fn)

    def init_nodemaker(self):
        default = self.get_config("client", "mutable.format", default="SDMF")
        if default.upper() == "MDMF":
            self.mutable_file_default = MDMF_VERSION
        else:
            self.mutable_file_default = SDMF_VERSION
        self.nodemaker = NodeMaker(self.storage_broker,
                                   self._secret_holder,
                                   self.get_history(),
                                   self.getServiceNamed("uploader"),
                                   self.terminator,
                                   self.get_encoding_parameters(),
                                   self.mutable_file_default,
                                   self._key_generator,
                                   self.blacklist)

    def get_history(self):
        return self.history

    def init_control(self):
        d = self.when_tub_ready()
        def _publish(res):
            c = ControlServer()
            c.setServiceParent(self)
            control_url = self.tub.registerReference(c)
            self.write_private_config("control.furl", control_url + "\n")
        d.addCallback(_publish)
        d.addErrback(log.err, facility="tahoe.init",
                     level=log.BAD, umid="d3tNXA")

    def init_helper(self):
        d = self.when_tub_ready()
        def _publish(self):
            self.helper = Helper(os.path.join(self.basedir, "helper"),
                                 self.storage_broker, self._secret_holder,
                                 self.stats_provider, self.history)
            # TODO: this is confusing. BASEDIR/private/helper.furl is created
            # by the helper. BASEDIR/helper.furl is consumed by the client
            # who wants to use the helper. I like having the filename be the
            # same, since that makes 'cp' work smoothly, but the difference
            # between config inputs and generated outputs is hard to see.
            helper_furlfile = os.path.join(self.basedir,
                                           "private", "helper.furl").encode(get_filesystem_encoding())
            self.tub.registerReference(self.helper, furlFile=helper_furlfile)
        d.addCallback(_publish)
        d.addErrback(log.err, facility="tahoe.init",
                     level=log.BAD, umid="K0mW5w")

    def init_key_gen(self, key_gen_furl):
        d = self.when_tub_ready()
        def _subscribe(self):
            self.tub.connectTo(key_gen_furl, self._got_key_generator)
        d.addCallback(_subscribe)
        d.addErrback(log.err, facility="tahoe.init",
                     level=log.BAD, umid="z9DMzw")

    def _got_key_generator(self, key_generator):
        self._key_generator.set_remote_generator(key_generator)
        key_generator.notifyOnDisconnect(self._lost_key_generator)

    def _lost_key_generator(self):
        self._key_generator.set_remote_generator(None)

    def set_default_mutable_keysize(self, keysize):
        self._key_generator.set_default_keysize(keysize)

    def init_web(self, webport):
        self.log("init_web(webport=%s)", args=(webport,))

        from allmydata.webish import WebishServer
        nodeurl_path = os.path.join(self.basedir, "node.url")
        staticdir_config = self.get_config("node", "web.static", "public_html").decode("utf-8")
        staticdir = abspath_expanduser_unicode(staticdir_config, base=self.basedir)
        ws = WebishServer(self, webport, nodeurl_path, staticdir)
        self.add_service(ws)

    def init_ftp_server(self):
        if self.get_config("ftpd", "enabled", False, boolean=True):
            accountfile = from_utf8_or_none(
                self.get_config("ftpd", "accounts.file", None))
            if accountfile:
                accountfile = abspath_expanduser_unicode(accountfile, base=self.basedir)
            accounturl = self.get_config("ftpd", "accounts.url", None)
            ftp_portstr = self.get_config("ftpd", "port", "8021")

            from allmydata.frontends import ftpd
            s = ftpd.FTPServer(self, accountfile, accounturl, ftp_portstr)
            s.setServiceParent(self)

    def init_sftp_server(self):
        if self.get_config("sftpd", "enabled", False, boolean=True):
            accountfile = from_utf8_or_none(
                self.get_config("sftpd", "accounts.file", None))
            if accountfile:
                accountfile = abspath_expanduser_unicode(accountfile, base=self.basedir)
            accounturl = self.get_config("sftpd", "accounts.url", None)
            sftp_portstr = self.get_config("sftpd", "port", "8022")
            pubkey_file = from_utf8_or_none(self.get_config("sftpd", "host_pubkey_file"))
            privkey_file = from_utf8_or_none(self.get_config("sftpd", "host_privkey_file"))

            from allmydata.frontends import sftpd
            s = sftpd.SFTPServer(self, accountfile, accounturl,
                                 sftp_portstr, pubkey_file, privkey_file)
            s.setServiceParent(self)

    def init_drop_uploader(self):
        if self.get_config("drop_upload", "enabled", False, boolean=True):
            if self.get_config("drop_upload", "upload.dircap", None):
                raise OldConfigOptionError("The [drop_upload]upload.dircap option is no longer supported; please "
                                           "put the cap in a 'private/drop_upload_dircap' file, and delete this option.")

            upload_dircap = self.get_or_create_private_config("drop_upload_dircap")
            local_dir_utf8 = self.get_config("drop_upload", "local.directory")

            try:
                from allmydata.frontends import drop_upload
                s = drop_upload.DropUploader(self, upload_dircap, local_dir_utf8)
                s.setServiceParent(self)
                s.startService()
            except Exception, e:
                self.log("couldn't start drop-uploader: %r", args=(e,))

    def _check_exit_trigger(self, exit_trigger_file):
        if os.path.exists(exit_trigger_file):
            mtime = os.stat(exit_trigger_file)[stat.ST_MTIME]
            if mtime > time.time() - 120.0:
                return
            else:
                self.log("%s file too old, shutting down" % (self.EXIT_TRIGGER_FILE,))
        else:
            self.log("%s file missing, shutting down" % (self.EXIT_TRIGGER_FILE,))
        reactor.stop()

    def get_encoding_parameters(self):
        return self.encoding_params

    def connected_to_introducer(self):
        if self.introducer_client:
            return self.introducer_client.connected_to_introducer()
        return False

    def get_renewal_secret(self): # this will go away
        return self._secret_holder.get_renewal_secret()

    def get_cancel_secret(self):
        return self._secret_holder.get_cancel_secret()

    def debug_wait_for_client_connections(self, num_clients):
        """Return a Deferred that fires (with None) when we have connections
        to the given number of peers. Useful for tests that set up a
        temporary test network and need to know when it is safe to proceed
        with an upload or download."""
        def _check():
            return len(self.storage_broker.get_connected_servers()) >= num_clients
        d = self.poll(_check, 0.5)
        d.addCallback(lambda res: None)
        return d


    # these four methods are the primitives for creating filenodes and
    # dirnodes. The first takes a URI and produces a filenode or (new-style)
    # dirnode. The other three create brand-new filenodes/dirnodes.

    def create_node_from_uri(self, write_uri, read_uri=None, deep_immutable=False, name="<unknown name>"):
        # This returns synchronously.
        # Note that it does *not* validate the write_uri and read_uri; instead we
        # may get an opaque node if there were any problems.
        return self.nodemaker.create_from_cap(write_uri, read_uri, deep_immutable=deep_immutable, name=name)

    def create_dirnode(self, initial_children={}, version=None):
        d = self.nodemaker.create_new_mutable_directory(initial_children, version=version)
        return d

    def create_immutable_dirnode(self, children, convergence=None):
        return self.nodemaker.create_immutable_directory(children, convergence)

    def create_mutable_file(self, contents=None, keysize=None, version=None):
        return self.nodemaker.create_mutable_file(contents, keysize,
                                                  version=version)

    def upload(self, uploadable):
        uploader = self.getServiceNamed("uploader")
        return uploader.upload(uploadable)
