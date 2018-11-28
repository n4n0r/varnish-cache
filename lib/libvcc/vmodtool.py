#!/usr/bin/env python
#
# Copyright (c) 2010-2016 Varnish Software
# All rights reserved.
#
# Author: Poul-Henning Kamp <phk@phk.freebsd.dk>
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR AND CONTRIBUTORS ``AS IS'' AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL AUTHOR OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
# OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
# SUCH DAMAGE.

"""
Read the vmod.vcc file (inputvcc) and produce:
    vmod_if.h -- Prototypes for the implementation
    vmod_if.c -- Magic glue & datastructures to make things a VMOD.
    vmod_${name}.rst -- Extracted documentation
"""

# This script should work with both Python 2 and Python 3.
from __future__ import print_function

import os
import sys
import re
import optparse
import unittest
import copy
import json
import hashlib

AMBOILERPLATE = '''
# Boilerplate generated by vmodtool.py - changes will be overwritten

AM_LDFLAGS  = $(AM_LT_LDFLAGS)

AM_CPPFLAGS = \\
\t-I$(top_srcdir)/include \\
\t-I$(top_srcdir)/bin/varnishd \\
\t-I$(top_builddir)/include

vmoddir = $(pkglibdir)/vmods
vmodtool = $(top_srcdir)/lib/libvcc/vmodtool.py
vmodtoolargs = --strict --boilerplate

vmod_LTLIBRARIES = libvmod_XXX.la

libvmod_XXX_la_CFLAGS = \\
\t@SAN_CFLAGS@

libvmod_XXX_la_LDFLAGS = \\
\t$(AM_LDFLAGS) \\
\t$(VMOD_LDFLAGS) \\
\t@SAN_LDFLAGS@

nodist_libvmod_XXX_la_SOURCES = vcc_if.c vcc_if.h

$(libvmod_XXX_la_OBJECTS): vcc_if.h

vcc_if.h vmod_XXX.rst vmod_XXX.man.rst: vcc_if.c

vcc_if.c: $(vmodtool) $(srcdir)/vmod.vcc
\t@PYTHON@ $(vmodtool) $(vmodtoolargs) $(srcdir)/vmod.vcc

EXTRA_DIST = vmod.vcc automake_boilerplate.am

CLEANFILES = $(builddir)/vcc_if.c $(builddir)/vcc_if.h \\
\t$(builddir)/vmod_XXX.rst \\
\t$(builddir)/vmod_XXX.man.rst

'''

PRIVS = {
    'PRIV_CALL':   "struct vmod_priv *",
    'PRIV_VCL':    "struct vmod_priv *",
    'PRIV_TASK':   "struct vmod_priv *",
    'PRIV_TOP':    "struct vmod_priv *",
}

CTYPES = {
    'ACL':         "VCL_ACL",
    'BACKEND':     "VCL_BACKEND",
    'BLOB':        "VCL_BLOB",
    'BODY':        "VCL_BODY",
    'BOOL':        "VCL_BOOL",
    'BYTES':       "VCL_BYTES",
    'DURATION':    "VCL_DURATION",
    'ENUM':        "VCL_ENUM",
    'HEADER':      "VCL_HEADER",
    'HTTP':        "VCL_HTTP",
    'INT':         "VCL_INT",
    'IP':          "VCL_IP",
    'PROBE':       "VCL_PROBE",
    'REAL':        "VCL_REAL",
    'STEVEDORE':   "VCL_STEVEDORE",
    'STRANDS':     "VCL_STRANDS",
    'STRING':      "VCL_STRING",
    'STRING_LIST': "const char *, ...",
    'TIME':        "VCL_TIME",
    'VOID':        "VCL_VOID",
}

CTYPES.update(PRIVS)

#######################################################################

def is_quoted(str):
    return len(str) > 2 and str[0] == str[-1] and str[0] in ('"', "'")

def unquote(str):
    assert is_quoted(str)
    return str[1:-1]

#######################################################################


def write_file_warning(fo, a, b, c):
    fo.write(a + "\n")
    fo.write(b + " NB:  This file is machine generated, DO NOT EDIT!\n")
    fo.write(b + "\n")
    fo.write(b + " Edit vmod.vcc and run make instead\n")
    fo.write(c + "\n\n")


def write_c_file_warning(fo):
    write_file_warning(fo, "/*", " *", " */")


def write_rst_file_warning(fo):
    write_file_warning(fo, "..", "..", "..")


def write_rst_hdr(fo, s, below="-", above=None):
    if above is not None:
        fo.write(above * len(s) + "\n")
    fo.write(s + "\n")
    if below is not None:
        fo.write(below * len(s) + "\n")

#######################################################################


def lwrap(s, width=64):
    """
    Wrap a C-prototype like string into a number of lines.
    """
    ll = []
    p = ""
    while len(s) > width:
        y = s[:width].rfind(',')
        if y == -1:
            y = s[:width].rfind('(')
        if y == -1:
            break
        ll.append(p + s[:y + 1])
        s = s[y + 1:].lstrip()
        p = "    "
    if s:
        ll.append(p + s)
    return "\n".join(ll) + "\n"


def fmt_cstruct(fo, mn, x):
    """
    Align fields in C struct
    """
    a = "\ttd_" + mn + "_" + x
    while len(a.expandtabs()) < 40:
        a += "\t"
    fo.write("%s*%s;\n" % (a, x))

#######################################################################


inputline = None


def err(txt, warn=True):
    if inputline is not None:
        print("While parsing line:\n\t", inputline)
    if opts.strict or not warn:
        print("ERROR: " + txt, file=sys.stderr)
        exit(1)
    else:
        print("WARNING: " + txt, file=sys.stderr)

#######################################################################


class CType(object):
    def __init__(self, wl, enums):
        self.nm = None
        self.defval = None
        self.spec = None
        self.opt = False

        self.vt = wl.pop(0)
        self.ct = CTYPES.get(self.vt)
        if self.ct is None:
            err("Expected type got '%s'" % self.vt, warn=False)
        if wl and wl[0] == "{":
            if self.vt != "ENUM":
                err("Only ENUMs take {...} specs", warn=False)
            self.add_spec(wl, enums)

    def __str__(self):
        s = "<" + self.vt
        if self.nm is not None:
            s += " " + self.nm
        if self.defval is not None:
            s += " VAL=" + self.defval
        if self.spec is not None:
            s += " SPEC=" + str(self.spec)
        return s + ">"

    def add_spec(self, wl, enums):
        assert self.vt == "ENUM"
        assert wl.pop(0) == "{"
        self.spec = []
        while True:
            x = wl.pop(0)
            if is_quoted(x):
                x = unquote(x)
            assert x
            self.spec.append(x)
            enums[x] = True
            w = wl.pop(0)
            if w == "}":
                break
            assert w == ","

    def vcl(self):
        if self.vt in ("STRING_LIST", "STRAND"):
            return "STRING"
        if self.spec is None:
            return self.vt
        return self.vt + " {" + ", ".join(self.spec) + "}"

    def synopsis(self):
        if self.vt in ("STRING_LIST", "STRAND"):
            return "STRING"
        return self.vt

    def json(self, jl):
        jl.append([self.vt])
        while jl[-1][-1] is None:
            jl[-1].pop(-1)

#######################################################################


class arg(CType):

    ''' Parse front of word list into argument '''

    def __init__(self, wl, argnames, enums, end):
        super(arg, self).__init__(wl, enums)

        if wl[0] == end:
            return

        x = wl.pop(0)
        if x in argnames:
            err("Duplicate argument name '%s'" % x, warn=False)
        argnames[x] = True
        self.nm = x

        if wl[0] == end:
            return

        x = wl.pop(0)
        if x != "=":
            err("Expected '=' got '%s'" % x, warn=False)

        x = wl.pop(0)
        if self.vt == "ENUM":
            if is_quoted(x):
                x = unquote(x)
        self.defval = x

    def json(self, jl):
        jl.append([self.vt, self.nm, self.defval, self.spec])
        if self.opt:
            jl[-1].append(True)
        while jl[-1][-1] is None:
            jl[-1].pop(-1)

#######################################################################


class ProtoType(object):
    def __init__(self, st, retval=True, prefix=""):
        self.st = st
        self.obj = None
        self.args = []
        self.argstruct = False
        wl = self.st.toks[1:]

        if retval:
            self.retval = CType(wl, st.vcc.enums)
        else:
            self.retval = CType(['VOID'], st.vcc.enums)

        self.bname = wl.pop(0)
        if not re.match("^[a-zA-Z.][a-zA-Z0-9_]*$", self.bname):
            err("%s(): Illegal name\n" % self.bname, warn=False)

        self.name = prefix + self.bname
        if not re.match('^[a-zA-Z_][a-zA-Z0-9_]*$', self.cname()):
            err("%s(): Illegal C-name\n" % self.cname(), warn=False)

        if len(wl) == 2 and wl[0] == '(' and wl[1] == ')':
            return

        if wl[0] != "(":
            err("Syntax error: Expected '(', got '%s'" % wl[0], warn=False)
        wl[0] = ','

        if wl[-1] != ")":
            err("Syntax error: Expected ')', got '%s'" % wl[-1], warn=False)
        wl[-1] = ','

        names = {}
        n = 0
        while wl:
            n += 1
            x = wl.pop(0)
            if x != ',':
                err("Expected ',' found '%s'" % x, warn=False)
            if not wl:
                break
            if wl[0] == '[':
                wl.pop(0)
                t = arg(wl, names, st.vcc.enums, ']')
                if t.nm is None:
                    err("Optional arguments must have names", warn=False)
                t.opt = True
                x = wl.pop(0)
                if x != ']':
                    err("Expected ']' found '%s'" % x, warn=False)
                self.argstruct = True
            else:
                t = arg(wl, names, st.vcc.enums, ',')
            if t.nm is None:
                t.nm2 = "arg%d" % n
            else:
                t.nm2 = t.nm
            self.args.append(t)

    def vcl_proto(self, short, pfx=""):
        if type(self.st) == s_method:
            pfx += pfx
        s = pfx
        if type(self.st) == s_object:
            s += "new x" + self.bname + " = "
        elif self.retval is not None:
            s += self.retval.vcl() + " "

        if type(self.st) == s_method:
            s += self.obj + self.bname + "("
        else:
            s += self.name + "("
        ll = []
        for i in self.args:
            if short:
                t = i.synopsis()
            else:
                t = i.vcl()
            if t in PRIVS:
                continue
            if i.nm is not None:
                t += " " + i.nm
            if not short:
                if i.defval is not None:
                    t += "=" + i.defval
            if i.opt:
                t = "[" + t + "]"
            ll.append(t)
        t = ",@".join(ll)
        if len(s + t) > 68 and not short:
            s += "\n" + pfx + pfx
            s += t.replace("@", "\n" + pfx + pfx)
            s += "\n" + pfx + ")"
        else:
            s += t.replace("@", " ") + ")"
        return s

    def rsthead(self, fo):
        s = self.vcl_proto(False)
        if len(s) < 60:
            write_rst_hdr(fo, s, '-')
        else:
            s = self.vcl_proto(True)
            if len(s) > 60:
                s = self.name + "(...)"
            write_rst_hdr(fo, s, '-')
            fo.write("\n::\n\n" + self.vcl_proto(False, pfx="   ") + "\n")

    def synopsis(self, fo, man):
        fo.write(self.vcl_proto(True, pfx="   ") + "\n")
        fo.write("  \n")

    def cname(self, pfx=False):
        r = self.name.replace(".", "_")
        if pfx:
            return self.st.vcc.sympfx + r
        return r

    def proto(self, args, name):
        s = self.retval.ct + " " + name + '('
        ll = args
        if self.argstruct:
            ll.append(self.argstructname() + "*")
        else:
            for i in self.args:
                ll.append(i.ct)
        s += ", ".join(ll)
        return s + ');'

    def typedef(self, args):
        tn = 'td_' + self.st.vcc.modname + '_' + self.cname()
        return "typedef " + self.proto(args, name=tn)

    def argstructname(self):
        return "struct %s_arg" % self.cname(True)

    def argstructure(self):
        s = "\n" + self.argstructname() + " {\n"
        for i in self.args:
            if i.opt:
                assert i.nm is not None
                s += "\tchar\t\t\tvalid_%s;\n" % i.nm
        for i in self.args:
            s += "\t" + i.ct
            if len(i.ct) < 8:
                s += "\t"
            if len(i.ct) < 16:
                s += "\t"
            s += "\t" + i.nm2 + ";\n"
        s += "};\n"
        return s

    def cstuff(self, args, where):
        s = ""
        if where == 'h':
            if self.argstruct:
                s += self.argstructure()
            s += lwrap(self.proto(args, self.cname(True)))
        elif where == 'c':
            s += lwrap(self.typedef(args))
        elif where == 'o':
            if self.argstruct:
                s += self.argstructure()
            s += lwrap(self.typedef(args))
        else:
            assert False
        return s

    def json(self, jl, cfunc):
        ll = []
        self.retval.json(ll)
        ll.append('Vmod_%s_Func.%s' % (self.st.vcc.modname, cfunc))
        if self.argstruct:
            ll.append(self.argstructname())
        else:
            ll.append("")
        for i in self.args:
            i.json(ll)
        jl.append(ll)

#######################################################################


class stanza(object):
    def __init__(self, toks, l0, doc, vcc):
        self.toks = toks
        self.line = l0
        while doc and doc[0] == '':
            doc.pop(0)
        while doc and doc[-1] == '':
            doc.pop(-1)
        self.doc = doc
        self.vcc = vcc
        self.rstlbl = None
        self.methods = None
        self.proto = None
        self.parse()

    def dump(self):
        print(type(self), self.line)

    def syntax(self):
        err("Syntax error.\n" +
            "\tShould be: " + self.__doc__.strip() + "\n" +
            "\tIs: " + " ".join(self.toks) + "\n",
            warn=False)

    def rstfile(self, fo, man):
        if self.rstlbl is not None:
            fo.write(".. _" + self.rstlbl + ":\n\n")

        self.rsthead(fo, man)
        fo.write("\n")
        self.rstmid(fo, man)
        fo.write("\n")
        self.rsttail(fo, man)
        fo.write("\n")

    def rsthead(self, fo, man):
        if self.proto is None:
            return
        self.proto.rsthead(fo)

    def rstmid(self, fo, man):
        fo.write("\n".join(self.doc) + "\n")

    def rsttail(self, fo, man):
        return

    def synopsis(self, fo, man):
        if self.proto is not None:
            self.proto.synopsis(fo, man)

    def cstuff(self, fo, where):
        return

    def cstruct(self, fo, define):
        return

    def json(self, jl):
        return

#######################################################################


class s_module(stanza):

    ''' $Module modname man_section description ... '''

    def parse(self):
        if len(self.toks) < 4:
            self.syntax()
        self.vcc.modname = self.toks[1]
        self.vcc.mansection = self.toks[2]
        if len(self.toks) == 4 and is_quoted(self.toks[3]):
            self.vcc.moddesc = unquote(self.toks[3])
        else:
            print("\nNOTICE: Please put $Module description in quotes.\n")
            self.vcc.moddesc = " ".join(self.toks[3:])
        self.rstlbl = "vmod_%s(%s)" % (
            self.vcc.modname,
            self.vcc.mansection
        )
        self.vcc.contents.append(self)

    def rsthead(self, fo, man):

        write_rst_hdr(fo, self.vcc.sympfx + self.vcc.modname, "=", "=")
        fo.write("\n")

        write_rst_hdr(fo, self.vcc.moddesc, "-", "-")

        fo.write("\n")
        fo.write(":Manual section: " + self.vcc.mansection + "\n")

        if self.vcc.auto_synopsis:
            fo.write("\n")
            write_rst_hdr(fo, "SYNOPSIS", "=")
            fo.write("\n")
            fo.write("\n::\n\n")
            fo.write('   import %s [from "path"] ;\n' % self.vcc.modname)
            fo.write("   \n")
            for c in self.vcc.contents:
                c.synopsis(fo, man)
            fo.write("\n")

    def rsttail(self, fo, man):

        if man:
            return

        write_rst_hdr(fo, "CONTENTS", "=")
        fo.write("\n")

        ll = []
        for i in self.vcc.contents[1:]:
            j = i.rstlbl
            if j is not None:
                ll.append([j.split("_", 1)[1], j])
            if i.methods is None:
                continue
            for x in i.methods:
                j = x.rstlbl
                ll.append([j.split("_", 1)[1], j])

        ll.sort()
        for i in ll:
            fo.write("* :ref:`%s`\n" % i[1])
        fo.write("\n")


class s_abi(stanza):

    ''' $ABI [strict|vrt] '''

    def parse(self):
        if len(self.toks) != 2:
            self.syntax()
        valid = {
            'strict': True,
            'vrt': False,
        }
        self.vcc.strict_abi = valid.get(self.toks[1])
        if self.vcc.strict_abi is None:
            err("Valid ABI types are 'strict' or 'vrt', got '%s'\n" %
                self.toks[1])
        self.vcc.contents.append(self)


class s_prefix(stanza):

    ''' $Prefix symbol '''

    def parse(self):
        if len(self.toks) != 2:
            self.syntax()
        self.vcc.sympfx = self.toks[1] + "_"
        self.vcc.contents.append(self)


class s_synopsis(stanza):

    ''' $Synopsis [auto|manual] '''

    def parse(self):
        if len(self.toks) != 2:
            self.syntax()
        valid = {
            'auto': True,
            'manual': False,
        }
        self.vcc.auto_synopsis = valid.get(self.toks[1])
        if self.vcc.auto_synopsis is None:
            err("Valid Synopsis values are 'auto' or 'manual', got '%s'\n" %
                self.toks[1])
        self.vcc.contents.append(self)


class s_event(stanza):

    ''' $Event function_name '''

    def parse(self):
        if len(self.toks) != 2:
            self.syntax()
        self.event_func = self.toks[1]
        self.vcc.contents.append(self)

    def rstfile(self, fo, man):
        if self.doc:
            err("Not emitting .RST for $Event %s\n" %
                self.event_func)

    def cstuff(self, fo, where):
        if where == 'h':
            fo.write("vmod_event_f %s;\n" % self.event_func)

    def cstruct(self, fo, define):
        if define:
            fo.write("\tvmod_event_f\t\t\t*_event;\n")
        else:
            fo.write("\t%s,\n" % self.event_func)

    def json(self, jl):
        jl.append([
            "$EVENT",
            "Vmod_%s_Func._event" % self.vcc.modname
        ])


class s_function(stanza):
    def parse(self):
        self.proto = ProtoType(self)
        self.rstlbl = "func_" + self.proto.name
        self.vcc.contents.append(self)

    def cstuff(self, fo, where):
        fo.write(self.proto.cstuff(['VRT_CTX'], where))

    def cstruct(self, fo, define):
        if define:
            fmt_cstruct(fo, self.vcc.modname, self.proto.cname())
        else:
            fo.write("\t" + self.proto.cname(pfx=True) + ",\n")

    def json(self, jl):
        jl.append(["$FUNC", "%s" % self.proto.name])
        self.proto.json(jl[-1], self.proto.cname())


class s_object(stanza):
    def parse(self):
        self.proto = ProtoType(self, retval=False)
        self.proto.obj = "x" + self.proto.name

        self.init = copy.copy(self.proto)
        self.init.name += '__init'

        self.fini = copy.copy(self.proto)
        self.fini.name += '__fini'
        self.fini.argstruct = False
        self.fini.args = []

        self.rstlbl = "obj_" + self.proto.name
        self.vcc.contents.append(self)
        self.methods = []

    def rsthead(self, fo, man):
        self.proto.rsthead(fo)

        fo.write("\n" + "\n".join(self.doc) + "\n\n")

        for i in self.methods:
            i.rstfile(fo, man)

    def rstmid(self, fo, man):
        return

    def synopsis(self, fo, man):
        self.proto.synopsis(fo, man)
        for i in self.methods:
            i.proto.synopsis(fo, man)

    def cstuff(self, fo, w):
        sn = self.vcc.sympfx + self.vcc.modname + "_" + self.proto.name
        fo.write("struct %s;\n" % sn)

        fo.write(self.init.cstuff(
            ['VRT_CTX', 'struct %s **' % sn, 'const char *'], w))
        fo.write(self.fini.cstuff(['struct %s **' % sn], w))
        for i in self.methods:
            fo.write(i.proto.cstuff(['VRT_CTX', 'struct %s *' % sn], w))
        fo.write("\n")

    def cstruct(self, fo, define):
        if define:
            fmt_cstruct(fo, self.vcc.modname, self.init.name)
            fmt_cstruct(fo, self.vcc.modname, self.fini.name)
        else:
            p = "\t" + self.vcc.sympfx
            fo.write(p + self.init.name + ",\n")
            fo.write(p + self.fini.name + ",\n")
        for i in self.methods:
            i.cstruct(fo, define)
        fo.write("\n")

    def json(self, jl):
        ll = [
            "$OBJ",
            self.proto.name,
            "struct %s%s_%s" %
            (self.vcc.sympfx, self.vcc.modname, self.proto.name),
        ]

        l2 = ["$INIT"]
        ll.append(l2)
        self.init.json(l2, self.init.name)

        l2 = ["$FINI"]
        ll.append(l2)
        self.fini.json(l2, self.fini.name)

        for i in self.methods:
            i.json(ll)

        jl.append(ll)

    def dump(self):
        super(s_object, self).dump()
        for i in self.methods:
            i.dump()

#######################################################################


class s_method(stanza):
    def parse(self):
        p = self.vcc.contents[-1]
        assert type(p) == s_object
        self.pfx = p.proto.name
        self.proto = ProtoType(self, prefix=self.pfx)
        if not self.proto.bname.startswith("."):
            err("$Method %s: Method names need to start with . (dot)"
                % self.proto.bname, warn=False)
        self.proto.obj = "x" + self.pfx
        self.rstlbl = "func_" + self.proto.name
        p.methods.append(self)

    def cstruct(self, fo, define):
        if define:
            fmt_cstruct(fo, self.vcc.modname, self.proto.cname())
        else:
            fo.write('\t' + self.proto.cname(pfx=True) + ",\n")

    def json(self, jl):
        jl.append(["$METHOD", self.proto.name[len(self.pfx)+1:]])
        self.proto.json(jl[-1], self.proto.cname())


#######################################################################

DISPATCH = {
    "Module":   s_module,
    "Prefix":   s_prefix,
    "ABI":      s_abi,
    "Event":    s_event,
    "Function": s_function,
    "Object":   s_object,
    "Method":   s_method,
    "Synopsis": s_synopsis,
}


class vcc(object):
    def __init__(self, inputvcc, rstdir, outputprefix):
        self.inputfile = inputvcc
        self.rstdir = rstdir
        self.pfx = outputprefix
        self.sympfx = "vmod_"
        self.contents = []
        self.commit_files = []
        self.copyright = ""
        self.enums = {}
        self.strict_abi = True
        self.auto_synopsis = True
        self.modname = None

    def openfile(self, fn):
        self.commit_files.append(fn)
        return open(fn + ".tmp", "w")

    def commit(self):
        for i in self.commit_files:
            os.rename(i + ".tmp", i)

    def parse(self):
        global inputline
        b = open(self.inputfile, "rb").read()
        a = "\n" + b.decode("utf-8")
        self.file_id = hashlib.sha256(b).hexdigest()
        s = a.split("\n$")
        self.copyright = s.pop(0).strip()
        while s:
            ss = re.split('\n([^\t ])', s.pop(0), maxsplit=1)
            toks = self.tokenize(ss[0])
            inputline = '$' + ' '.join(toks)
            c = ss[0].split()
            d = "".join(ss[1:])
            m = DISPATCH.get(toks[0])
            if m is None:
                err("Unknown stanza $%s" % toks[0], warn=False)
            m(toks, [c[0], " ".join(c[1:])], d.split('\n'), self)
            inputline = None

    def tokenize(self, str, seps=None, quotes=None):
        if seps is None:
            seps = "[](){},="
        if quotes is None:
            quotes = '"' + "'"
        quote = None
        out = []
        i = 0
        inside = False
        while i < len(str):
            c = str[i]
            # print("T", [c], quote, inside, i)
            i += 1
            if quote is not None and c == quote:
                inside = False
                quote = None
                out[-1] += c
            elif quote is not None:
                out[-1] += c
            elif c.isspace():
                inside = False
            elif seps.find(c) >= 0:
                inside = False
                out.append(c)
            elif quotes.find(c) >= 0:
                quote = c
                out.append(c)
            elif inside:
                out[-1] += c
            else:
                out.append(c)
                inside = True
        #print("TOK", [str])
        #for i in out:
        #    print("\t", [i])
        return out


    def rst_copyright(self, fo):
        write_rst_hdr(fo, "COPYRIGHT", "=")
        fo.write("\n::\n\n")
        a = self.copyright
        a = a.replace("\n#", "\n ")
        if a[:2] == "#\n":
            a = a[2:]
        if a[:3] == "#-\n":
            a = a[3:]
        fo.write(a + "\n")

    def rstfile(self, man=False):
        fn = os.path.join(self.rstdir, "vmod_" + self.modname)
        if man:
            fn += ".man"
        fn += ".rst"
        fo = self.openfile(fn)
        write_rst_file_warning(fo)
        fo.write(".. role:: ref(emphasis)\n\n")

        for i in self.contents:
            i.rstfile(fo, man)

        if self.copyright:
            self.rst_copyright(fo)

        fo.close()

    def amboilerplate(self):
        fo = self.openfile("automake_boilerplate.am")
        fo.write(AMBOILERPLATE.replace("XXX", self.modname))
        fo.close()

    def hfile(self):
        fn = self.pfx + ".h"
        fo = self.openfile(fn)
        write_c_file_warning(fo)
        fo.write("#ifndef VDEF_H_INCLUDED\n")
        fo.write('#  error "Include vdef.h first"\n')
        fo.write("#endif\n")
        fo.write("#ifndef VRT_H_INCLUDED\n")
        fo.write('#  error "Include vrt.h first"\n')
        fo.write("#endif\n")
        fo.write("\n")

        for j in sorted(self.enums):
            fo.write("extern VCL_ENUM %senum_%s;\n" % (self.sympfx, j))
        fo.write("\n")

        for j in self.contents:
            j.cstuff(fo, 'h')
        fo.close()

    def cstruct(self, fo, csn):
        fo.write("\n%s {\n" % csn)
        for j in self.contents:
            j.cstruct(fo, True)
        for j in sorted(self.enums):
            fo.write("\tVCL_ENUM\t\t\t*enum_%s;\n" % j)
        fo.write("};\n")

    def cstruct_init(self, fo, csn):
        fo.write("\nstatic const %s Vmod_Func = {\n" % csn)
        for j in self.contents:
            j.cstruct(fo, False)
        fo.write("\n")
        for j in sorted(self.enums):
            fo.write("\t&%senum_%s,\n" % (self.sympfx, j))
        fo.write("};\n")

    def json(self, fo):
        jl = [["$VMOD", "1.0"]]
        for j in self.contents:
            j.json(jl)

        bz = bytearray(json.dumps(jl, separators=(",", ":")),
                       encoding="ascii") + b"\0"
        fo.write("\nstatic const char Vmod_Json[%d] = {\n" % len(bz))
        t = "\t"
        for i in bz:
            t += "%d," % i
            if len(t) >= 69:
                fo.write(t + "\n")
                t = "\t"
        if len(t) > 1:
            fo.write(t[:-1])
        fo.write("\n};\n\n")
        for i in json.dumps(jl, indent=2, separators=(',', ': ')).split("\n"):
            j = "// " + i
            if len(j) > 72:
                fo.write(j[:72] + "[...]\n")
            else:
                fo.write(j + "\n")
        fo.write("\n")

    def vmod_data(self, fo):
        vmd = "Vmod_%s_Data" % self.modname
        for i in (714, 759, 765):
            fo.write("\n/*lint -esym(%d, %s) */\n" % (i, vmd))
        fo.write("\nextern const struct vmod_data %s;\n" % vmd)
        fo.write("\nconst struct vmod_data %s = {\n" % vmd)
        if self.strict_abi:
            fo.write("\t.vrt_major =\t0,\n")
            fo.write("\t.vrt_minor =\t0,\n")
        else:
            fo.write("\t.vrt_major =\tVRT_MAJOR_VERSION,\n")
            fo.write("\t.vrt_minor =\tVRT_MINOR_VERSION,\n")
        fo.write('\t.name =\t\t"%s",\n' % self.modname)
        fo.write('\t.func =\t\t&Vmod_Func,\n')
        fo.write('\t.func_len =\tsizeof(Vmod_Func),\n')
        fo.write('\t.proto =\tVmod_Proto,\n')
        fo.write('\t.json =\t\tVmod_Json,\n')
        fo.write('\t.abi =\t\tVMOD_ABI_Version,\n')
        fo.write("\t.file_id =\t\"%s\",\n" % self.file_id)
        fo.write("};\n")

    def cfile(self):
        fno = self.pfx + ".c"
        fo = self.openfile(fno)
        fnx = fno + ".tmp2"
        fx = open(fnx, "w")

        write_c_file_warning(fo)

        fo.write('#include "config.h"\n')
        fo.write('#include <stdio.h>\n')
        for i in ["vdef", "vrt", self.pfx, "vmod_abi"]:
            fo.write('#include "%s.h"\n' % i)
        fo.write("\n")

        for j in sorted(self.enums):
            fo.write('VCL_ENUM %senum_%s = "%s";\n' % (self.sympfx, j, j))
        fo.write("\n")

        for i in self.contents:
            if type(i) == s_object:
                i.cstuff(fo, 'c')
                i.cstuff(fx, 'o')

        fx.write("/* Functions */\n")
        for i in self.contents:
            if type(i) == s_function:
                i.cstuff(fo, 'c')
                i.cstuff(fx, 'o')

        csn = "Vmod_%s_Func" % self.modname
        scsn = "struct " + csn

        self.cstruct(fo, scsn)
        self.cstruct(fx, scsn)

        fo.write("\n/*lint -esym(754, " + csn + "::*) */\n")
        self.cstruct_init(fo, scsn)

        fx.close()

        fo.write("\nstatic const char Vmod_Proto[] =\n")
        for i in open(fnx):
            fo.write('\t"%s\\n"\n' % i.rstrip())
        fo.write('\t"static struct %s %s;";\n' % (csn, csn))

        os.remove(fnx)

        self.json(fo)

        self.vmod_data(fo)

        fo.close()

#######################################################################


def runmain(inputvcc, rstdir, outputprefix):

    v = vcc(inputvcc, rstdir, outputprefix)
    v.parse()

    v.rstfile(man=False)
    v.rstfile(man=True)
    v.hfile()
    v.cfile()
    if opts.boilerplate:
        v.amboilerplate()

    v.commit()


if __name__ == "__main__":
    usagetext = "Usage: %prog [options] <vmod.vcc>"
    oparser = optparse.OptionParser(usage=usagetext)

    oparser.add_option('-b', '--boilerplate', action='store_true',
                       default=False,
                       help="Be strict when parsing the input file")
    oparser.add_option('-N', '--strict', action='store_true', default=False,
                       help="Be strict when parsing the input file")
    oparser.add_option('-o', '--output', metavar="prefix", default='vcc_if',
                       help='Output file prefix (default: "vcc_if")')
    oparser.add_option('-w', '--rstdir', metavar="directory", default='.',
                       help='Where to save the generated RST files ' +
                       '(default: ".")')
    oparser.add_option('', '--runtests', action='store_true', default=False,
                       dest="runtests", help=optparse.SUPPRESS_HELP)
    (opts, args) = oparser.parse_args()

    if opts.runtests:
        # Pop off --runtests, pass remaining to unittest.
        del sys.argv[1]
        unittest.main()
        exit()

    i_vcc = None
    if len(args) == 1 and os.path.exists(args[0]):
        i_vcc = args[0]
    elif os.path.exists("vmod.vcc"):
        if not i_vcc:
            i_vcc = "vmod.vcc"
    else:
        print("ERROR: No vmod.vcc file supplied or found.", file=sys.stderr)
        oparser.print_help()
        exit(-1)

    runmain(i_vcc, opts.rstdir, opts.output)
