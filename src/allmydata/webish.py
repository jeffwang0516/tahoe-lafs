
from twisted.application import service, strports
from twisted.web import static, resource, server, html
from twisted.python import util, log
from nevow import inevow, rend, loaders, appserver, url, tags as T
from nevow.static import File as nevow_File # TODO: merge with static.File?
from allmydata.util import idlib
from allmydata.uri import unpack_uri
from allmydata.interfaces import IDownloadTarget
from allmydata.vdrive import FileNode, DirectoryNode
from allmydata import upload
from zope.interface import implements, Interface
import urllib
from formless import annotate, webform

def getxmlfile(name):
    return loaders.xmlfile(util.sibpath(__file__, "web/%s" % name))

class IClient(Interface):
    pass

def get_downloader_service(ctx):
    return IClient(ctx).getServiceNamed("downloader")
def get_uploader_service(ctx):
    return IClient(ctx).getServiceNamed("uploader")

class Welcome(rend.Page):
    addSlash = True
    docFactory = getxmlfile("welcome.xhtml")

    def data_version(self, ctx, data):
        v = IClient(ctx).get_versions()
        return "tahoe: %s, zfec: %s, foolscap: %s, twisted: %s" % \
               (v['allmydata'], v['zfec'], v['foolscap'], v['twisted'])

    def data_my_nodeid(self, ctx, data):
        return idlib.b2a(IClient(ctx).nodeid)
    def data_introducer_furl(self, ctx, data):
        return IClient(ctx).introducer_furl
    def data_connected_to_introducer(self, ctx, data):
        if IClient(ctx).connected_to_introducer():
            return "yes"
        return "no"
    def data_connected_to_vdrive(self, ctx, data):
        if IClient(ctx).connected_to_vdrive():
            return "yes"
        return "no"
    def data_num_peers(self, ctx, data):
        #client = inevow.ISite(ctx)._client
        client = IClient(ctx)
        return len(list(client.get_all_peerids()))

    def data_peers(self, ctx, data):
        d = []
        client = IClient(ctx)
        for nodeid in sorted(client.get_all_peerids()):
            row = (idlib.b2a(nodeid),)
            d.append(row)
        return d

    def render_row(self, ctx, data):
        (nodeid_a,) = data
        ctx.fillSlots("peerid", nodeid_a)
        return ctx.tag

    def render_global_vdrive(self, ctx, data):
        if self.has_global_vdrive:
            return T.p["To view the global shared filestore, ",
                       T.a(href="../global_vdrive")["Click Here!"],
                       ]
        return T.p["vdrive.furl not specified (or vdrive server not "
                   "responding), no vdrive available."]

    def render_my_vdrive(self, ctx, data):
        if self.has_my_vdrive:
            return T.p["To view your personal private non-shared filestore, ",
                       T.a(href="../my_vdrive")["Click Here!"],
                       ]
        return T.p["personal vdrive not available."]

    # this is a form where users can download files by URI

    def bind_download(self, ctx):
        uriarg = annotate.Argument("uri",
                                   annotate.String("URI of file to download: "))
        namearg = annotate.Argument("filename",
                                    annotate.String("Filename to download as: "))
        ctxarg = annotate.Argument("ctx", annotate.Context())
        meth = annotate.Method(arguments=[uriarg, namearg, ctxarg],
                               label="Download File by URI")
        # buttons always use value=data.label
        # MethodBindingRenderer uses value=(data.action or data.label)
        return annotate.MethodBinding("download", meth, action="Download")

    def download(self, uri, filename, ctx):
        log.msg("webish downloading URI")
        target = url.here.sibling("download_uri").add("uri", uri)
        if filename:
            target = target.add("filename", filename)
        return target

    def render_forms(self, ctx, data):
        return webform.renderForms()


class Directory(rend.Page):
    addSlash = True
    docFactory = getxmlfile("directory.xhtml")

    def __init__(self, dirnode, dirname):
        self._dirnode = dirnode
        self._dirname = dirname

    def childFactory(self, ctx, name):
        if name.startswith("freeform"): # ick
            return None
        if self._dirname == "/":
            dirname = "/" + name
        else:
            dirname = self._dirname + "/" + name
        d = self._dirnode.get(name)
        def _got_child(res):
            if isinstance(res, FileNode):
                dl = get_downloader_service(ctx)
                return Downloader(dl, name, res)
            elif isinstance(res, DirectoryNode):
                return Directory(res, dirname)
            else:
                raise RuntimeError("what is this %s" % res)
        d.addCallback(_got_child)
        return d

    def render_title(self, ctx, data):
        return ctx.tag["Directory of '%s':" % self._dirname]

    def render_header(self, ctx, data):
        return "Directory of '%s':" % self._dirname

    def data_share_url(self, ctx, data):
        return self._dirnode.furl

    def data_children(self, ctx, data):
        d = self._dirnode.list()
        return d

    def render_row(self, ctx, data):
        name, target = data
        if isinstance(target, FileNode):
            # file
            dlurl = urllib.quote(name)
            ctx.fillSlots("filename",
                          T.a(href=dlurl)[html.escape(name)])
            ctx.fillSlots("type", "FILE")
            uri = target.uri
            dl_uri_url = url.root.child("download_uri").child(uri)
            # add a filename= query argument to give it a Content-Type
            dl_uri_url = dl_uri_url.add("filename", name)
            ctx.fillSlots("uri", T.a(href=dl_uri_url)[html.escape(uri)])

            #extract and display file size
            ctx.fillSlots("size", unpack_uri(uri)['size'])

            # this creates a button which will cause our child__delete method
            # to be invoked, which deletes the file and then redirects the
            # browser back to this directory
            del_url = url.here.child("_delete")
            #del_url = del_url.add("uri", target.uri)
            del_url = del_url.add("name", name)
            delete = T.form(action=del_url, method="post")[
                T.input(type='submit', value='del', name="del"),
                ]
            ctx.fillSlots("delete", delete)
        elif isinstance(target, DirectoryNode):
            # directory
            subdir_url = urllib.quote(name)
            ctx.fillSlots("filename",
                          T.a(href=subdir_url)[html.escape(name)])
            ctx.fillSlots("type", "DIR")
            ctx.fillSlots("size", "-")
            ctx.fillSlots("uri", "-")

            del_url = url.here.child("_delete")
            del_url = del_url.add("name", name)
            delete = T.form(action=del_url, method="post")[
                T.input(type='submit', value='del', name="del"),
                ]
            ctx.fillSlots("delete", delete)
        else:
            raise RuntimeError("unknown thing %s" % (target,))
        return ctx.tag

    def render_forms(self, ctx, data):
        return webform.renderForms()

    def render_results(self, ctx, data):
        req = inevow.IRequest(ctx)
        if "results" in req.args:
            return req.args["results"]
        else:
            return ""

    def bind_upload(self, ctx):
        """upload1"""
        # Note: this comment is no longer accurate, as it reflects the older
        # (apparently deprecated) formless.autocallable /
        # annotate.TypedInterface approach.

        # Each method gets a box. The string in the autocallable(action=)
        # argument is put on the border of the box, as well as in the submit
        # button. The top-most contents of the box are the method's
        # docstring, if any. Each row contains a string for the argument
        # followed by the argument's input box. If you do not provide an
        # action= argument to autocallable, the method name is capitalized
        # and used instead.
        up = annotate.FileUpload(label="Choose a file to upload: ",
                                 required=True,
                                 requiredFailMessage="Do iT!")
        contentsarg = annotate.Argument("contents", up)

        privateUpload = annotate.Radio(label="Private?", choices=["Yes"])
        privatearg = annotate.Argument("privateupload", privateUpload)

        ctxarg = annotate.Argument("ctx", annotate.Context())
        meth = annotate.Method(arguments=[contentsarg, privatearg, ctxarg],
                               label="Upload File to this directory")
        return annotate.MethodBinding("upload", meth, action="Upload")

    def uploadprivate(self, filename, uri):
        message = "webish upload complete, filename %s %s" % (filename, uri)
        log.msg(message)
        return url.here.add("filename", filename).add("results", message)

    def upload(self, contents, privateupload, ctx):
        # contents is a cgi.FieldStorage instance
        log.msg("starting webish upload")

        uploader = get_uploader_service(ctx)
        d = uploader.upload(upload.FileHandle(contents.file))
        name = contents.filename
        def _uploaded(uri):
            if privateupload:
                return self.uploadprivate(name, uri)
            else:
                return self._dirnode.add(name, FileNode(uri))
        d.addCallback(_uploaded)
        def _done(res):
            log.msg("webish upload complete")
            return res
        d.addCallback(_done)
        return d # TODO: huh?
        return url.here.add("results",
                            "upload of '%s' complete!" % contents.filename)

    def bind_mkdir(self, ctx):
        """Make new directory 1"""
        namearg = annotate.Argument("name",
                                    annotate.String("New directory name: "))
        meth = annotate.Method(arguments=[namearg], label="Make New Subdirectory")
        return annotate.MethodBinding("mkdir", meth, action="Create Directory")

    def mkdir(self, name):
        """mkdir2"""
        log.msg("making new webish directory: %s" % (name,))
        d = self._dirnode.create_empty_directory(name)
        def _done(res):
            log.msg("webish mkdir complete")
            return res
        d.addCallback(_done)
        return d

    def bind_mount(self, ctx):
        namearg = annotate.Argument("name",
                                    annotate.String("Name to place incoming directory: "))
        furlarg = annotate.Argument("furl",
                                    annotate.String("FURL of Shared Directory"))
        meth = annotate.Method(arguments=[namearg, furlarg],
                               label="Add Shared Directory")
        return annotate.MethodBinding("mount", meth,
                                      action="Mount Shared Directory")

    def mount(self, name, furl):
        d = self._dirnode.attach_shared_directory(name, furl)
        #d.addCallback(lambda done: url.here.child(name))
        return d

    def child__delete(self, ctx):
        # perform the delete, then redirect back to the directory page
        args = inevow.IRequest(ctx).args
        name = args["name"][0]
        d = self._dirnode.remove(name)
        d.addCallback(lambda done: url.here.up())
        return d

class WebDownloadTarget:
    implements(IDownloadTarget)
    def __init__(self, req):
        self._req = req
    def open(self):
        pass
    def write(self, data):
        self._req.write(data)
    def close(self):
        self._req.finish()
    def fail(self):
        self._req.finish()
    def register_canceller(self, cb):
        pass
    def finish(self):
        pass

class TypedFile(static.File):
    # serve data from a named file, but using a Content-Type derived from a
    # different filename
    isLeaf = True
    def __init__(self, path, requested_filename):
        static.File.__init__(self, path)
        gte = static.getTypeAndEncoding
        self.type, self.encoding = gte(requested_filename,
                                       self.contentTypes,
                                       self.contentEncodings,
                                       self.defaultType)

class Downloader(resource.Resource):
    def __init__(self, downloader, name, filenode):
        self._downloader = downloader
        self._name = name
        assert isinstance(filenode, FileNode)
        self._filenode = filenode

    def render(self, ctx):
        req = inevow.IRequest(ctx)
        gte = static.getTypeAndEncoding
        type, encoding = gte(self._name,
                             static.File.contentTypes,
                             static.File.contentEncodings,
                             defaultType="text/plain")
        req.setHeader("content-type", type)
        if encoding:
            req.setHeader('content-encoding', encoding)

        self._filenode.download(WebDownloadTarget(req))
        return server.NOT_DONE_YET



class Root(rend.Page):
    def locateChild(self, ctx, segments):
        if segments[0] == "download_uri":
            req = inevow.IRequest(ctx)
            dl = get_downloader_service(ctx)
            filename = "unknown_filename"
            if "filename" in req.args:
                filename = req.args["filename"][0]
            if len(segments) > 1:
                # http://host/download_uri/URIGOESHERE
                uri = segments[1]
            elif "uri" in req.args:
                # http://host/download_uri?uri=URIGOESHERE
                uri = req.args["uri"][0]
            else:
                return rend.NotFound
            child = Downloader(dl, filename, FileNode(uri, IClient(ctx)))
            return child, ()
        return rend.Page.locateChild(self, ctx, segments)

    child_webform_css = webform.defaultCSS
    child_tahoe_css = nevow_File(util.sibpath(__file__, "web/tahoe.css"))

    child_welcome = Welcome()


class WebishServer(service.MultiService):
    name = "webish"

    def __init__(self, webport):
        service.MultiService.__init__(self)
        self.root = Root()
        self.root.child_welcome.has_global_vdrive = False
        self.root.child_welcome.has_my_vdrive = False
        placeholder = static.Data("sorry, still initializing", "text/plain")
        self.root.putChild("vdrive", placeholder)
        self.root.putChild("", url.here.child("welcome"))#Welcome())
                           
        self.site = site = appserver.NevowSite(self.root)
        s = strports.service(webport, site)
        s.setServiceParent(self)
        self.listener = s # stash it so the tests can query for the portnum

    def startService(self):
        service.MultiService.startService(self)
        # to make various services available to render_* methods, we stash a
        # reference to the client on the NevowSite. This will be available by
        # adapting the 'context' argument to a special marker interface named
        # IClient.
        self.site.remember(self.parent, IClient)
        # I thought you could do the same with an existing interface, but
        # apparently 'ISite' does not exist
        #self.site._client = self.parent

    def set_vdrive_root(self, root):
        self.root.putChild("global_vdrive", Directory(root, "/"))
        self.root.child_welcome.has_global_vdrive = True
        # I tried doing it this way and for some reason it didn't seem to work
        #print "REMEMBERING", self.site, dl, IDownloader
        #self.site.remember(dl, IDownloader)

    def set_my_vdrive_root(self, my_vdrive):
        self.root.putChild("my_vdrive", Directory(my_vdrive, "~"))
        self.root.child_welcome.has_my_vdrive = True

