# Copyright 2014-present PlatformIO <contact@platformio.org>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import

import atexit
import re
import sys
from glob import glob
from os import environ, remove
from os.path import basename, isfile, join
from tempfile import mkstemp

from SCons.Action import Action
from SCons.Script import ARGUMENTS

from platformio import util


class InoToCPPConverter(object):

    PROTOTYPE_RE = re.compile(r"""^(
        ([a-z_\d]+\*?\s+){1,2}      # return type
        ([a-z_\d]+\s*)              # name of prototype
        \([a-z_,\.\*\&\[\]\s\d]*\)  # arguments
        )\s*\{                      # must end with {
        """, re.X | re.M | re.I)
    DETECTMAIN_RE = re.compile(r"void\s+(setup|loop)\s*\(", re.M | re.I)
    PROTOPTRS_TPLRE = r"\([^&\(]*&(%s)[^\)]*\)"

    def __init__(self, env):
        self.env = env
        self._main_ino = None

    def is_main_node(self, contents):
        return self.DETECTMAIN_RE.search(contents)

    def convert(self, nodes):
        contents = self.merge(nodes)
        if not contents:
            return
        return self.process(contents)

    def merge(self, nodes):
        assert nodes
        lines = []
        for node in nodes:
            contents = node.get_text_contents()
            _lines = [
                '# 1 "%s"' % node.get_path().replace("\\", "/"), contents
            ]
            if self.is_main_node(contents):
                lines = _lines + lines
                self._main_ino = node.get_path()
            else:
                lines.extend(_lines)

        if not self._main_ino:
            self._main_ino = nodes[0].get_path()

        return "\n".join(["#include <Arduino.h>"] + lines) if lines else None

    def process(self, contents):
        out_file = self._main_ino + ".cpp"
        assert self._gcc_preprocess(contents, out_file)
        with open(out_file) as fp:
            contents = fp.read()
        contents = self._join_multiline_strings(contents)
        with open(out_file, "w") as fp:
            fp.write(self.append_prototypes(contents))
        return out_file

    def _gcc_preprocess(self, contents, out_file):
        tmp_path = mkstemp()[1]
        with open(tmp_path, "w") as fp:
            fp.write(contents)
        self.env.Execute(
            self.env.VerboseAction(
                '$CXX -o "{0}" -x c++ -fpreprocessed -dD -E "{1}"'.format(
                    out_file, tmp_path), "Converting " + basename(
                        out_file[:-4])))
        atexit.register(_delete_file, tmp_path)
        return isfile(out_file)

    def _join_multiline_strings(self, contents):
        if "\\\n" not in contents:
            return contents
        newlines = []
        linenum = 0
        stropen = False
        for line in contents.split("\n"):
            _linenum = self._parse_preproc_line_num(line)
            if _linenum is not None:
                linenum = _linenum
            else:
                linenum += 1

            if line.endswith("\\"):
                if line.startswith('"'):
                    stropen = True
                    newlines.append(line[:-1])
                    continue
                elif stropen:
                    newlines[len(newlines) - 1] += line[:-1]
                    continue
            elif stropen and line.endswith('";'):
                newlines[len(newlines) - 1] += line
                stropen = False
                newlines.append('#line %d "%s"' %
                                (linenum, self._main_ino.replace("\\", "/")))
                continue

            newlines.append(line)

        return "\n".join(newlines)

    @staticmethod
    def _parse_preproc_line_num(line):
        if not line.startswith("#"):
            return None
        tokens = line.split(" ", 3)
        if len(tokens) > 2 and tokens[1].isdigit():
            return int(tokens[1])
        return None

    def _parse_prototypes(self, contents):
        prototypes = []
        reserved_keywords = set(["if", "else", "while"])
        for match in self.PROTOTYPE_RE.finditer(contents):
            if (set([match.group(2).strip(), match.group(3).strip()]) &
                    reserved_keywords):
                continue
            prototypes.append(match)
        return prototypes

    def _get_total_lines(self, contents):
        total = 0
        for line in contents.split("\n")[::-1]:
            linenum = self._parse_preproc_line_num(line)
            if linenum is not None:
                return total + linenum
            total += 1
        return total

    def append_prototypes(self, contents):
        prototypes = self._parse_prototypes(contents)
        if not prototypes:
            return contents

        prototype_names = set([m.group(3).strip() for m in prototypes])
        split_pos = prototypes[0].start()
        match_ptrs = re.search(self.PROTOPTRS_TPLRE %
                               ("|".join(prototype_names)),
                               contents[:split_pos], re.M)
        if match_ptrs:
            split_pos = contents.rfind("\n", 0, match_ptrs.start())

        result = []
        result.append(contents[:split_pos].strip())
        result.append("%s;" % ";\n".join([m.group(1) for m in prototypes]))
        result.append('#line %d "%s"' %
                      (self._get_total_lines(contents[:split_pos]),
                       self._main_ino.replace("\\", "/")))
        result.append(contents[split_pos:].strip())
        return "\n".join(result)


def ConvertInoToCpp(env):
    ino_nodes = (env.Glob(join("$PROJECTSRC_DIR", "*.ino")) +
                 env.Glob(join("$PROJECTSRC_DIR", "*.pde")))
    c = InoToCPPConverter(env)
    out_file = c.convert(ino_nodes)

    atexit.register(_delete_file, out_file)


def _delete_file(path):
    try:
        if isfile(path):
            remove(path)
    except:  # pylint: disable=bare-except
        pass


def DumpIDEData(env):

    def get_includes(env_):
        includes = []

        for item in env_.get("CPPPATH", []):
            includes.append(env_.subst(item))

        # installed libs
        for lb in env.GetLibBuilders():
            includes.extend(lb.get_inc_dirs())

        # includes from toolchains
        p = env.PioPlatform()
        for name in p.get_installed_packages():
            if p.get_package_type(name) != "toolchain":
                continue
            toolchain_dir = p.get_package_dir(name)
            toolchain_incglobs = [
                join(toolchain_dir, "*", "include*"),
                join(toolchain_dir, "lib", "gcc", "*", "*", "include*")
            ]
            for g in toolchain_incglobs:
                includes.extend(glob(g))

        return includes

    def get_defines(env_):
        defines = []
        # global symbols
        for item in env_.get("CPPDEFINES", []):
            if isinstance(item, list) or isinstance(item, tuple):
                item = "=".join(item)
            defines.append(env_.subst(item).replace('\\"', '"'))

        # special symbol for Atmel AVR MCU
        if env['PIOPLATFORM'] == "atmelavr":
            defines.append(
                "__AVR_%s__" % env.BoardConfig().get("build.mcu").upper()
                .replace("ATMEGA", "ATmega").replace("ATTINY", "ATtiny"))
        return defines

    LINTCCOM = "$CFLAGS $CCFLAGS $CPPFLAGS $_CPPDEFFLAGS"
    LINTCXXCOM = "$CXXFLAGS $CCFLAGS $CPPFLAGS $_CPPDEFFLAGS"
    env_ = env.Clone()

    data = {
        "defines": get_defines(env_),
        "includes": get_includes(env_),
        "cc_flags": env_.subst(LINTCCOM),
        "cxx_flags": env_.subst(LINTCXXCOM),
        "cc_path": util.where_is_program(
            env_.subst("$CC"), env_.subst("${ENV['PATH']}")),
        "cxx_path": util.where_is_program(
            env_.subst("$CXX"), env_.subst("${ENV['PATH']}"))
    }

    # https://github.com/platformio/platformio-atom-ide/issues/34
    _new_defines = []
    for item in env_.get("CPPDEFINES", []):
        if isinstance(item, list) or isinstance(item, tuple):
            item = "=".join(item)
        item = item.replace('\\"', '"')
        if " " in item:
            _new_defines.append(item.replace(" ", "\\\\ "))
        else:
            _new_defines.append(item)
    env_.Replace(CPPDEFINES=_new_defines)

    data.update({
        "cc_flags": env_.subst(LINTCCOM),
        "cxx_flags": env_.subst(LINTCXXCOM)
    })

    return data


def GetCompilerType(env):
    try:
        sysenv = environ.copy()
        sysenv['PATH'] = str(env['ENV']['PATH'])
        result = util.exec_command([env.subst("$CC"), "-v"], env=sysenv)
    except OSError:
        return None
    if result['returncode'] != 0:
        return None
    output = "".join([result['out'], result['err']]).lower()
    if "clang" in output and "LLVM" in output:
        return "clang"
    elif "gcc" in output:
        return "gcc"
    return None


def GetActualLDScript(env):
    script = None
    for f in env.get("LINKFLAGS", []):
        if f.startswith("-Wl,-T"):
            script = env.subst(f[6:].replace('"', "").strip())
            if isfile(script):
                return script
            for d in env.get("LIBPATH", []):
                path = join(env.subst(d), script)
                if isfile(path):
                    return path

    if script:
        sys.stderr.write(
            "Error: Could not find '%s' LD script in LDPATH '%s'\n" %
            (script, env.subst("$LIBPATH")))
        env.Exit(1)

    return None


def VerboseAction(_, act, actstr):
    if int(ARGUMENTS.get("PIOVERBOSE", 0)):
        return act
    else:
        return Action(act, actstr)


def exists(_):
    return True


def generate(env):
    env.AddMethod(ConvertInoToCpp)
    env.AddMethod(DumpIDEData)
    env.AddMethod(GetCompilerType)
    env.AddMethod(GetActualLDScript)
    env.AddMethod(VerboseAction)
    return env
