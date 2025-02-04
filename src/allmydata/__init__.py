"""
Decentralized storage grid.

community web site: U{https://tahoe-lafs.org/}
"""

class PackagingError(EnvironmentError):
    """
    Raised when there is an error in packaging of Tahoe-LAFS or its
    dependencies which makes it impossible to proceed safely.
    """
    pass

__version__ = "unknown"
try:
    from allmydata._version import __version__
except ImportError:
    # We're running in a tree that hasn't run update_version, and didn't
    # come with a _version.py, so we don't know what our version is.
    # This should not happen very often.
    pass

full_version = "unknown"
branch = "unknown"
try:
    from allmydata._version import full_version, branch
except ImportError:
    # We're running in a tree that hasn't run update_version, and didn't
    # come with a _version.py, so we don't know what our full version or
    # branch is. This should not happen very often.
    pass

__appname__ = "unknown"
try:
    from allmydata._appname import __appname__
except ImportError:
    # We're running in a tree that hasn't run "./setup.py".  This shouldn't happen.
    pass

# __full_version__ is the one that you ought to use when identifying yourself in the
# "application" part of the Tahoe versioning scheme:
# https://tahoe-lafs.org/trac/tahoe-lafs/wiki/Versioning
__full_version__ = __appname__ + '/' + str(__version__)

import os, platform, re, subprocess, sys, traceback
_distributor_id_cmdline_re = re.compile("(?:Distributor ID:)\s*(.*)", re.I)
_release_cmdline_re = re.compile("(?:Release:)\s*(.*)", re.I)

_distributor_id_file_re = re.compile("(?:DISTRIB_ID\s*=)\s*(.*)", re.I)
_release_file_re = re.compile("(?:DISTRIB_RELEASE\s*=)\s*(.*)", re.I)

global _distname,_version
_distname = None
_version = None

def get_linux_distro():
    """ Tries to determine the name of the Linux OS distribution name.

    First, try to parse a file named "/etc/lsb-release".  If it exists, and
    contains the "DISTRIB_ID=" line and the "DISTRIB_RELEASE=" line, then return
    the strings parsed from that file.

    If that doesn't work, then invoke platform.dist().

    If that doesn't work, then try to execute "lsb_release", as standardized in
    2001:

    http://refspecs.freestandards.org/LSB_1.0.0/gLSB/lsbrelease.html

    The current version of the standard is here:

    http://refspecs.freestandards.org/LSB_3.2.0/LSB-Core-generic/LSB-Core-generic/lsbrelease.html

    that lsb_release emitted, as strings.

    Returns a tuple (distname,version). Distname is what LSB calls a
    "distributor id", e.g. "Ubuntu".  Version is what LSB calls a "release",
    e.g. "8.04".

    A version of this has been submitted to python as a patch for the standard
    library module "platform":

    http://bugs.python.org/issue3937
    """
    global _distname,_version
    if _distname and _version:
        return (_distname, _version)

    try:
        etclsbrel = open("/etc/lsb-release", "rU")
        for line in etclsbrel:
            m = _distributor_id_file_re.search(line)
            if m:
                _distname = m.group(1).strip()
                if _distname and _version:
                    return (_distname, _version)
            m = _release_file_re.search(line)
            if m:
                _version = m.group(1).strip()
                if _distname and _version:
                    return (_distname, _version)
    except EnvironmentError:
        pass

    (_distname, _version) = platform.dist()[:2]
    if _distname and _version:
        return (_distname, _version)

    if os.path.isfile("/usr/bin/lsb_release") or os.path.isfile("/bin/lsb_release"):
        try:
            p = subprocess.Popen(["lsb_release", "--all"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            rc = p.wait()
            if rc == 0:
                for line in p.stdout.readlines():
                    m = _distributor_id_cmdline_re.search(line)
                    if m:
                        _distname = m.group(1).strip()
                        if _distname and _version:
                            return (_distname, _version)

                    m = _release_cmdline_re.search(p.stdout.read())
                    if m:
                        _version = m.group(1).strip()
                        if _distname and _version:
                            return (_distname, _version)
        except EnvironmentError:
            pass

    if os.path.exists("/etc/arch-release"):
        return ("Arch_Linux", "")

    return (_distname,_version)

def get_platform():
    # Our version of platform.platform(), telling us both less and more than the
    # Python Standard Library's version does.
    # We omit details such as the Linux kernel version number, but we add a
    # more detailed and correct rendition of the Linux distribution and
    # distribution-version.
    if "linux" in platform.system().lower():
        return platform.system()+"-"+"_".join(get_linux_distro())+"-"+platform.machine()+"-"+"_".join([x for x in platform.architecture() if x])
    else:
        return platform.platform()


from allmydata.util import verlib
def normalized_version(verstr, what=None):
    try:
        suggested = verlib.suggest_normalized_version(verstr) or verstr
        return verlib.NormalizedVersion(suggested)
    except verlib.IrrationalVersionError:
        raise
    except StandardError:
        cls, value, trace = sys.exc_info()
        raise PackagingError, ("could not parse %s due to %s: %s"
                               % (what or repr(verstr), cls.__name__, value)), trace

def get_openssl_version():
    try:
        from OpenSSL import SSL
        return extract_openssl_version(SSL)
    except Exception:
        return ("unknown", None, None)

def extract_openssl_version(ssl_module):
    openssl_version = ssl_module.SSLeay_version(ssl_module.SSLEAY_VERSION)
    if openssl_version.startswith('OpenSSL '):
        openssl_version = openssl_version[8 :]

    (version, _, comment) = openssl_version.partition(' ')

    try:
        openssl_cflags = ssl_module.SSLeay_version(ssl_module.SSLEAY_CFLAGS)
        if '-DOPENSSL_NO_HEARTBEATS' in openssl_cflags.split(' '):
            comment += ", no heartbeats"
    except Exception:
        pass

    return (version, None, comment if comment else None)

def get_package_versions_and_locations():
    import warnings
    from _auto_deps import package_imports, global_deprecation_messages, deprecation_messages, \
        runtime_warning_messages, warning_imports, ignorable

    def package_dir(srcfile):
        return os.path.dirname(os.path.dirname(os.path.normcase(os.path.realpath(srcfile))))

    # pkg_resources.require returns the distribution that pkg_resources attempted to put
    # on sys.path, which can differ from the one that we actually import due to #1258,
    # or any other bug that causes sys.path to be set up incorrectly. Therefore we
    # must import the packages in order to check their versions and paths.

    # This is to suppress all UserWarnings and various DeprecationWarnings and RuntimeWarnings
    # (listed in _auto_deps.py).

    warnings.filterwarnings("ignore", category=UserWarning, append=True)

    for msg in global_deprecation_messages + deprecation_messages:
        warnings.filterwarnings("ignore", category=DeprecationWarning, message=msg, append=True)
    for msg in runtime_warning_messages:
        warnings.filterwarnings("ignore", category=RuntimeWarning, message=msg, append=True)
    try:
        for modulename in warning_imports:
            try:
                __import__(modulename)
            except ImportError:
                pass
    finally:
        # Leave suppressions for UserWarnings and global_deprecation_messages active.
        for ign in runtime_warning_messages + deprecation_messages:
            warnings.filters.pop()

    packages = []
    pkg_resources_vers_and_locs = dict()

    if not hasattr(sys, 'frozen'):
        import pkg_resources
        from _auto_deps import install_requires

        pkg_resources_vers_and_locs = dict([(p.project_name.lower(), (str(p.version), p.location))
                                            for p in pkg_resources.require(install_requires)])

    def get_version(module):
        if hasattr(module, '__version__'):
            return str(getattr(module, '__version__'))
        elif hasattr(module, 'version'):
            ver = getattr(module, 'version')
            if isinstance(ver, tuple):
                return '.'.join(map(str, ver))
            else:
                return str(ver)
        else:
            return 'unknown'

    for pkgname, modulename in [(__appname__, 'allmydata')] + package_imports:
        if modulename:
            try:
                __import__(modulename)
                module = sys.modules[modulename]
            except ImportError:
                etype, emsg, etrace = sys.exc_info()
                trace_info = (etype, str(emsg), ([None] + traceback.extract_tb(etrace))[-1])
                packages.append( (pkgname, (None, None, trace_info)) )
            else:
                comment = None
                if pkgname == __appname__:
                    comment = "%s: %s" % (branch, full_version)
                elif pkgname == 'setuptools' and hasattr(module, '_distribute'):
                    # distribute does not report its version in any module variables
                    comment = 'distribute'
                ver = get_version(module)
                loc = package_dir(module.__file__)
                if ver == "unknown" and pkgname in pkg_resources_vers_and_locs:
                    (pr_ver, pr_loc) = pkg_resources_vers_and_locs[pkgname]
                    if loc == os.path.normcase(os.path.realpath(pr_loc)):
                        ver = pr_ver
                packages.append( (pkgname, (ver, loc, comment)) )
        elif pkgname == 'python':
            packages.append( (pkgname, (platform.python_version(), sys.executable, None)) )
        elif pkgname == 'platform':
            packages.append( (pkgname, (get_platform(), None, None)) )
        elif pkgname == 'OpenSSL':
            packages.append( (pkgname, get_openssl_version()) )

    cross_check_errors = []

    if len(pkg_resources_vers_and_locs) > 0:
        imported_packages = set([p.lower() for (p, _) in packages])
        extra_packages = []

        for pr_name, (pr_ver, pr_loc) in pkg_resources_vers_and_locs.iteritems():
            if pr_name not in imported_packages and pr_name not in ignorable:
                extra_packages.append( (pr_name, (pr_ver, pr_loc, "according to pkg_resources")) )

        cross_check_errors = cross_check(pkg_resources_vers_and_locs, packages)
        packages += extra_packages

    return packages, cross_check_errors


def check_requirement(req, vers_and_locs):
    # We support only conjunctions of <=, >=, !=, and ==.

    reqlist = req.split(',')
    name = reqlist[0].split('<=')[0].split('>=')[0].split('!=')[0].split('==')[0].strip(' ').split('[')[0]
    if name not in vers_and_locs:
        raise PackagingError("no version info for %s" % (name,))
    if req.strip(' ') == name:
        return
    (actual, location, comment) = vers_and_locs[name]
    if actual is None:
        # comment is (type, message, (filename, line number, function name, text)) for the original ImportError
        raise ImportError("for requirement %r: %s" % (req, comment))
    if actual == 'unknown':
        return
    try:
        actualver = normalized_version(actual, what="actual version %r of %s from %r" %
                                               (actual, name, location))
        matched = match_requirement(req, reqlist, actualver)
    except verlib.IrrationalVersionError:
        # meh, it probably doesn't matter
        return

    if not matched:
        msg = ("We require %s, but could only find version %s.\n" % (req, actual))
        if location and location != 'unknown':
            msg += "The version we found is from %r.\n" % (location,)
        msg += ("To resolve this problem, uninstall that version, either using your\n"
                "operating system's package manager or by moving aside the directory.")
        raise PackagingError(msg)


def match_requirement(req, reqlist, actualver):
    for r in reqlist:
        s = r.split('<=')
        if len(s) == 2:
            required = s[1].strip(' ')
            if not (actualver <= normalized_version(required, what="required maximum version %r in %r" % (required, req))):
                return False  # maximum requirement not met
        else:
            s = r.split('>=')
            if len(s) == 2:
                required = s[1].strip(' ')
                if not (actualver >= normalized_version(required, what="required minimum version %r in %r" % (required, req))):
                    return False  # minimum requirement not met
            else:
                s = r.split('!=')
                if len(s) == 2:
                    required = s[1].strip(' ')
                    if not (actualver != normalized_version(required, what="excluded version %r in %r" % (required, req))):
                        return False  # not-equal requirement not met
                else:
                    s = r.split('==')
                    if len(s) == 2:
                        required = s[1].strip(' ')
                        if not (actualver == normalized_version(required, what="exact version %r in %r" % (required, req))):
                            return False  # equal requirement not met
                    else:
                        raise PackagingError("no version info or could not understand requirement %r" % (req,))

    return True


def cross_check(pkg_resources_vers_and_locs, imported_vers_and_locs_list):
    """This function returns a list of errors due to any failed cross-checks."""

    from _auto_deps import not_import_versionable

    errors = []
    not_pkg_resourceable = ['python', 'platform', __appname__.lower(), 'openssl']

    for name, (imp_ver, imp_loc, imp_comment) in imported_vers_and_locs_list:
        name = name.lower()
        if name not in not_pkg_resourceable:
            if name not in pkg_resources_vers_and_locs:
                if name == "setuptools" and "distribute" in pkg_resources_vers_and_locs:
                    pr_ver, pr_loc = pkg_resources_vers_and_locs["distribute"]
                    if not (os.path.normpath(os.path.realpath(pr_loc)) == os.path.normpath(os.path.realpath(imp_loc))
                            and imp_comment == "distribute"):
                        errors.append("Warning: dependency 'setuptools' found to be version %r of 'distribute' from %r "
                                      "by pkg_resources, but 'import setuptools' gave version %r [%s] from %r. "
                                      "A version mismatch is expected, but a location mismatch is not."
                                      % (pr_ver, pr_loc, imp_ver, imp_comment or 'probably *not* distribute', imp_loc))
                else:
                    errors.append("Warning: dependency %r (version %r imported from %r) was not found by pkg_resources."
                                  % (name, imp_ver, imp_loc))
                continue

            pr_ver, pr_loc = pkg_resources_vers_and_locs[name]
            if imp_ver is None and imp_loc is None:
                errors.append("Warning: dependency %r could not be imported. pkg_resources thought it should be possible "
                              "to import version %r from %r.\nThe exception trace was %r."
                              % (name, pr_ver, pr_loc, imp_comment))
                continue

            # If the pkg_resources version is identical to the imported version, don't attempt
            # to normalize them, since it is unnecessary and may fail (ticket #2499).
            if imp_ver != 'unknown' and pr_ver == imp_ver:
                continue

            try:
                pr_normver = normalized_version(pr_ver)
            except verlib.IrrationalVersionError:
                continue
            except Exception, e:
                errors.append("Warning: version number %r found for dependency %r by pkg_resources could not be parsed. "
                              "The version found by import was %r from %r. "
                              "pkg_resources thought it should be found at %r. "
                              "The exception was %s: %s"
                              % (pr_ver, name, imp_ver, imp_loc, pr_loc, e.__class__.__name__, e))
            else:
                if imp_ver == 'unknown':
                    if name not in not_import_versionable:
                        errors.append("Warning: unexpectedly could not find a version number for dependency %r imported from %r. "
                                      "pkg_resources thought it should be version %r at %r."
                                      % (name, imp_loc, pr_ver, pr_loc))
                else:
                    try:
                        imp_normver = normalized_version(imp_ver)
                    except verlib.IrrationalVersionError:
                        continue
                    except Exception, e:
                        errors.append("Warning: version number %r found for dependency %r (imported from %r) could not be parsed. "
                                      "pkg_resources thought it should be version %r at %r. "
                                      "The exception was %s: %s"
                                      % (imp_ver, name, imp_loc, pr_ver, pr_loc, e.__class__.__name__, e))
                    else:
                        if pr_ver == 'unknown' or (pr_normver != imp_normver):
                            if not os.path.normpath(os.path.realpath(pr_loc)) == os.path.normpath(os.path.realpath(imp_loc)):
                                errors.append("Warning: dependency %r found to have version number %r (normalized to %r, from %r) "
                                              "by pkg_resources, but version %r (normalized to %r, from %r) by import."
                                              % (name, pr_ver, str(pr_normver), pr_loc, imp_ver, str(imp_normver), imp_loc))

    return errors


_vers_and_locs_list, _cross_check_errors = get_package_versions_and_locations()


def get_error_string(errors, debug=False):
    from allmydata._auto_deps import install_requires

    msg = "\n%s\n" % ("\n".join(errors),)
    if debug:
        msg += ("\n"
                "For debugging purposes, the PYTHONPATH was\n"
                "  %r\n"
                "install_requires was\n"
                "  %r\n"
                "sys.path after importing pkg_resources was\n"
                "  %s\n"
                % (os.environ.get('PYTHONPATH'), install_requires, (os.pathsep+"\n  ").join(sys.path)) )
    return msg

def check_all_requirements():
    """This function returns a list of errors due to any failed checks."""

    from allmydata._auto_deps import install_requires

    fatal_errors = []

    # We require at least 2.6 on all platforms.
    # (On Python 3, we'll have failed long before this point.)
    if sys.version_info < (2, 6):
        try:
            version_string = ".".join(map(str, sys.version_info))
        except Exception:
            version_string = repr(sys.version_info)
        fatal_errors.append("Tahoe-LAFS currently requires Python v2.6 or greater (but less than v3), not %s"
                            % (version_string,))

    vers_and_locs = dict(_vers_and_locs_list)
    for requirement in install_requires:
        try:
            check_requirement(requirement, vers_and_locs)
        except (ImportError, PackagingError), e:
            fatal_errors.append("%s: %s" % (e.__class__.__name__, e))

    if fatal_errors:
        raise PackagingError(get_error_string(fatal_errors + _cross_check_errors, debug=True))

check_all_requirements()


def get_package_versions():
    return dict([(k, v) for k, (v, l, c) in _vers_and_locs_list])

def get_package_locations():
    return dict([(k, l) for k, (v, l, c) in _vers_and_locs_list])

def get_package_versions_string(show_paths=False, debug=False):
    res = []
    for p, (v, loc, comment) in _vers_and_locs_list:
        info = str(p) + ": " + str(v)
        if comment:
            info = info + " [%s]" % str(comment)
        if show_paths:
            info = info + " (%s)" % str(loc)
        res.append(info)

    output = "\n".join(res) + "\n"

    if _cross_check_errors:
        output += get_error_string(_cross_check_errors, debug=debug)

    return output
