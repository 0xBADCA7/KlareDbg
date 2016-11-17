from qira_base import *
import qira_config
import qira_analysis

import os
import shutil
import sys
import subprocess
import threading
import time
import collections
from hashlib import sha1

from subprocess import (Popen, PIPE)
import json

import struct
import qiradb

import arch

# new home of static2
sys.path.append(qira_config.BASEDIR+"/static2")
import static2
def which(prog):
  try:
    cmd = ["which", prog]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    res = p.stdout.readlines()
    if len(res) == 0:
      raise Exception("image not found")
    return os.path.realpath(res[0].strip())
  except:
    # fallback mode, look for the image straight up
    if os.path.isfile(prog):
      return os.path.realpath(prog)
    else:
      raise Exception("image not found")

# things that don't cross the fork
class Program:
  def __init__(self, image, binary, args=[], qemu_args=[]):
    # create the logs dir
    try:
      os.mkdir(qira_config.TRACE_FILE_BASE)
    except:
      pass

    # call which to match the behavior of strace and gdb
    self.image = which(image)
    self.binary = which(binary)
    #self.proghash = sha1(open(self.image, "rb").read()).hexdigest()
    print "*** image file is",self.image
    print "*** binary file is",self.binary

    # this is always initted, as it's the tag repo
    self.static = static2.Static(self.binary)

    # init static
    if qira_config.WITH_STATIC:
      threading.Thread(target=self.static.process).start()

    # no traces yet
    self.traces = {}
    self.runnable = False

    self.memory = 1500
    # bring this back
    if self.image != "/tmp/qira_image":
      try:
        os.unlink("/tmp/qira_image")
      except:
        pass
      try:
        os.symlink(os.path.realpath(self.image), "/tmp/qira_image")
      except:
        pass

    #XXX: Are these arguments necessary?
    #self.defaultargs = ["-strace", "-D", "/dev/null", "-d", "in_asm", "-singlestep"]+qemu_args
    self.defaultargs = ["-m", str(self.memory), "-drive", "file=%s" % self.image,
                        "-chardev", "socket,path=/tmp/qga.sock,server,nowait,id=qga0",
                        "-device", "virtio-serial",
                        "-device", "virtserialport,chardev=qga0,name=org.qemu.guest_agent.0",
                        "-monitor", "stdio",
                        "-d", "in_asm"]
    self.identify_guest_image()

  def identify_guest_image(self):
    qemu_dir = os.path.dirname(os.path.realpath(__file__))+"/../tracers/qemu/"

    # pmaps is global, but updated by the traces
    progdat = open(self.binary, "rb").read(0x800)

    CPU_TYPE_ARM = "\x0C"
    CPU_TYPE_ARM64 = "\x01\x00\x00\x0C"

    CPU_SUBTYPE_ARM_ALL = "\x00"
    CPU_SUBTYPE_ARM_V4T = "\x05"
    CPU_SUBTYPE_ARM_V6 = "\x06"
    CPU_SUBTYPE_ARM_V5TEJ = "\x07"
    CPU_SUBTYPE_ARM_XSCALE = "\x08"
    CPU_SUBTYPE_ARM_V7 = "\x09"
    CPU_SUBTYPE_ARM_V7F = "\x0A"
    CPU_SUBTYPE_ARM_V7S = "\x0B"
    CPU_SUBTYPE_ARM_V7K = "\x0C"
    CPU_SUBTYPE_ARM_V6M = "\x0E"
    CPU_SUBTYPE_ARM_V7M = "\x0F"
    CPU_SUBTYPE_ARM_V7EM = "\x10"

    CPU_SUBTYPE_ARM = [
                         CPU_SUBTYPE_ARM_V4T,
                         CPU_SUBTYPE_ARM_V6,
                         CPU_SUBTYPE_ARM_V5TEJ,
                         CPU_SUBTYPE_ARM_XSCALE,
                         CPU_SUBTYPE_ARM_V7,
                         CPU_SUBTYPE_ARM_V7F,
                         CPU_SUBTYPE_ARM_V7K,
                         CPU_SUBTYPE_ARM_V6M,
                         CPU_SUBTYPE_ARM_V7M,
                         CPU_SUBTYPE_ARM_V7EM
                      ]

    CPU_SUBTYPE_ARM64 = [
                         CPU_SUBTYPE_ARM_ALL,
                         CPU_SUBTYPE_ARM_V7S
                        ]

    MACHO_MAGIC = "\xFE\xED\xFA\xCE"
    MACHO_CIGAM = "\xCE\xFA\xED\xFE"
    MACHO_MAGIC_64 = "\xFE\xED\xFA\xCF"
    MACHO_CIGAM_64 = "\xCF\xFA\xED\xFE"
    MACHO_FAT_MAGIC = "\xCA\xFE\xBA\xBE"
    MACHO_FAT_CIGAM = "\xBE\xBA\xFE\xCA"
    MACHO_P200_FAT_MAGIC = "\xCA\xFE\xD0\x0D"
    MACHO_P200_FAT_CIGAM = "\x0D\xD0\xFE\xCA"

    # Linux binaries
    if progdat[0:4] == "\x7FELF":
      # get file type
      self.fb = struct.unpack("H", progdat[0x12:0x14])[0]   # e_machine
      if self.fb == 0x3e:
        self.tregs = arch.X64REGS
        self.qirabinary = qemu_dir + "qira-x86_64"

      self.qirabinary = os.path.realpath(self.qirabinary)
      print "**** using",self.qirabinary
      self.runnable = True

  def clear(self):
    # probably always good to do except in development of middleware
    #print "*** deleting old runs"
    #self.delete_old_runs()

    # getting asm from qemu
    self.create_asm_file()

  def create_asm_file(self):
    if os.name == "nt":
      return
    # try:
    #   os.unlink("/tmp/qira_asm")
    # except:
    #   pass
    open("/tmp/qira_asm", "a").close()
    self.qira_asm_file = open("/tmp/qira_asm", "r")

  def read_asm_file(self):
    if os.name == "nt":
      return
    print "read_asm_file\n"
    dat = self.qira_asm_file.read()
    print dat
    if len(dat) == 0:
      return
    cnt = 0
    for d in dat.split("\n"):
      thumb = False
      if len(d) == 0:
        continue
      # hacks
      try:
        if self.fb == 0x28:
          #thumb bit in front
          addr = int(d.split(" ")[0][1:].strip(":"), 16)
        else:
          addr = int(d.split(" ")[0].strip(":"), 16)
      except:
        continue
      if self.fb == 0x28:
        thumb_flag = d[0]
        if thumb_flag == 't':
          thumb = True
          # override the arch since it's thumb, clear invalid tag
          del self.static[addr]['instruction']
          self.static[addr]['arch'] = "thumb"
        elif thumb_flag == 'n':
          thumb = False
        else:
          #print "*** Invalid thumb flag at beginning of instruction"
          pass
        inst = d[d.rfind("  ")+2:]
      elif self.fb == 0xb7:   # aarch64
        inst = d[d.rfind("     ")+5:]
      else:
        inst = d[d.find(":")+2:]
      cnt += 1

      # trigger disasm
      d = self.static[addr]['instruction']
    #sys.stdout.write("%d..." % cnt); sys.stdout.flush()

  def delete_old_runs(self):
    # delete the logs
    shutil.rmtree(qira_config.TRACE_FILE_BASE)
    os.mkdir(qira_config.TRACE_FILE_BASE)

  def get_maxclnum(self):
    ret = {}
    for t in self.traces:
      ret[t] = [self.traces[t].db.get_minclnum(), self.traces[t].db.get_maxclnum()]
    return ret

  def get_pmaps(self):
    ret = {}
    for t in self.traces:
      pm = self.traces[t].db.get_pmaps()
      for a in pm:
        if a not in ret:
          ret[a] = pm[a]
        elif ret[a] == "memory":
          ret[a] = pm[a]

    # fix for numberless js
    rret = {}
    for k in ret:
      rret[ghex(k)] = ret[k]
    return rret

  def add_trace(self, fn, i):
    self.traces[i] = Trace(fn, i, self, self.tregs[1], len(self.tregs[0]), self.tregs[2])
    return self.traces[i]

  def execqira(self, args=[], shouldfork=True):
    if self.runnable == False:
      return
    if shouldfork:
      if os.fork() != 0:
        return
    eargs = [self.qirabinary]+self.defaultargs
    print "***",' '.join(eargs)
    #os.execvp(eargs[0], eargs)


class Trace:
  def __init__(self, fn, forknum, program, r1, r2, r3):
    self.forknum = forknum
    self.image = program
    self.db = qiradb.Trace(fn, forknum, r1, r2, r3)
    self.load_base_memory()

    # analysis stuff
    self.maxclnum = None
    self.minclnum = None
    self.flow = None
    self.dmap = None
    self.maxd = 0
    self.analysisready = False
    self.picture = None
    self.needs_update = False
    self.strace = []
    self.mapped = []
    print "Init Trace"
    threading.Thread(target=self.analysis_thread).start()

  def fetch_raw_memory(self, clnum, address, ln):
    return ''.join(map(chr, self.fetch_memory(clnum, address, ln).values()))

  # proxy the db call and fill in base memory
  def fetch_memory(self, clnum, address, ln):
    mem = self.db.fetch_memory(clnum, address, ln)
    dat = {}
    for i in range(ln):
      # we don't rebase the memory anymore, important for numberless
      ri = address+i
      if mem[i] & 0x100:
        dat[i] = mem[i]&0xFF
      else:
        try:
          dat[i] = ord(self.image.static.memory(ri, 1)[0])
        except:
          pass
          #dat[i] = 0 #XXX is this correct behavior?
    return dat

  def read_strace_file(self):
    try:
      f = open(qira_config.TRACE_FILE_BASE+str(int(self.forknum))+"_strace").read()
    except:
      return "no strace"

    f = ''.join(filter(lambda x: ord(x) < 0x80, f))
    ret = []
    files = {}
    for ff in f.split("\n"):
      if ff == '':
        continue
      ff = ff.split(" ")
      try:
        clnum = int(ff[0])
      except:
        continue
      # i think this filter isn't so useful now
      pid = int(ff[1])
      sc = " ".join(ff[2:])
      try:
        return_code = int(sc.split(") = ")[1].split(" ")[0], 0)
        fxn = sc.split("(")[0]
        if fxn == "open" and return_code != -1:
          firststr = sc.split('\"')[1]
          files[return_code] = firststr
        elif fxn[0:4] == "mmap":
          args = sc.split(",")
          sz = int(args[1], 0)
          fil = int(args[4], 0)
          off = int(args[5].split(")")[0], 0)
          mapp = (files[fil], sz, off, return_code)
          if mapp not in self.mapped:
            # if it fails once, don't try again
            self.mapped.append(mapp)
            try:
              try:
                f = open(os.environ['QEMU_LD_PREFIX']+"/"+files[fil])
              except:
                f = open(files[fil])
              alldat = f.read()

              if fxn == "mmap2":
                off = 4096*off # offset argument is in terms of pages for mmap2()
                # is it safe to assume 4096 byte pages?

              st = "*** mapping %s %s sz:0x%x off:0x%x @ 0x%X" % (sha1(alldat).hexdigest(), files[fil], sz, off, return_code)
              print st,
              dat = alldat[off:off+sz]

              self.image.static.add_memory_chunk(return_code, dat)
              print "done"
            except Exception, e:
              print e

      except:
        pass
      ret.append({"clnum": clnum, "pid":pid, "sc": sc})

    self.strace = ret

  def analysis_thread(self):
    print "*** started analysis_thread"
    while 1:
      time.sleep(0.2)
      # so this is done poorly, analysis can be incremental
      if self.maxclnum == None or self.db.get_maxclnum() != self.maxclnum:
        self.analysisready = False
        minclnum = self.db.get_minclnum()
        maxclnum = self.db.get_maxclnum()
        self.image.read_asm_file()
        #self.flow = qira_analysis.get_instruction_flow(self, self.image, minclnum, maxclnum)
        #self.dmap = qira_analysis.get_hacked_depth_map(self.flow, self.image)
        #qira_analysis.analyse_calls(self)

        # hacky pin offset problem fix
        # hpo = len(self.dmap)-(maxclnum-minclnum)
        # if hpo == 2:
        #   self.dmap = self.dmap[1:]
        #
        # self.maxd = max(self.dmap)
        # self.picture = qira_analysis.get_vtimeline_picture(self, minclnum, maxclnum)
        self.minclnum = minclnum
        self.maxclnum = maxclnum
        self.needs_update = True

        #print "analysis is ready"

  def load_base_memory(self):
    def get_forkbase_from_log(n):
      ret = struct.unpack("i", open(qira_config.TRACE_FILE_BASE+str(n)).read(0x18)[0x10:0x14])[0]
      if ret == -1:
        return n
      else:
        return get_forkbase_from_log(ret)

    try:
      forkbase = get_forkbase_from_log(self.forknum)
      print "*** using base %d for %d" % (forkbase, self.forknum)
      f = open(qira_config.TRACE_FILE_BASE+str(forkbase)+"_base")
    except Exception, e:
      print "*** base file issue",e
      # done
      return

    # Use any bundled images first. The structure of the images directory is:
    # _images/
    #   urlencoded%20image.dll
    #   or%20maybe%20a%20folder.dll/
    #     0000C000
    #     100008000
    # where a folder is like a sparsefile with chunks of data at it's hex-offset-named
    # subfiles. The reason for this sparsefile stuff is that OS X has non-contigous
    # loaded images, so we compensate by having each "file" actually be a chunk of
    # address space, which in theory could be very large. (The correct solution of
    # storing just the image file along with the regions data isn't well exposed
    # by Pin at this time, and would require explicit mach-o parsing and stuff.)
    img_map = {}
    images_dir = qira_config.TRACE_FILE_BASE+str(self.forknum)+"_images"
    if os.path.isdir(images_dir):
      try:
        from urllib import unquote
        for image in os.listdir(images_dir):
          if os.path.isfile(images_dir+"/"+image):
            img_map[unquote(image)] = {0: images_dir+"/"+image}
          else: # It's a directory
            off_map = {}
            for offset in os.listdir(images_dir+"/"+image):
              off_map[int(offset, 16)] = images_dir+"/"+image+"/"+offset
            img_map[unquote(image)] = off_map
      except Exception, e:
        print "Exception while dealing with _images/:", e

    for ln in f.read().split("\n"):
      ln = ln.split(" ")
      if len(ln) < 3:
        continue
      (ss, se) = ln[0].split("-")
      ss = int(ss, 16)
      se = int(se, 16)
      offset = int(ln[1], 16)
      fn = ' '.join(ln[2:])

      try:
        if fn in img_map:
          off = max(i for i in img_map[fn].iter_keys() if i <= offset)
          with open(img_map[fn][off]) as f:
            f.seek(offset-off)
            dat = f.read(se-ss)
        else:
          with open(fn) as f:
            f.seek(offset)
            dat = f.read(se-ss)
      except Exception, e:
        print "Failed to get", fn, "offset", offset, ":", e
        continue
      self.image.static.add_memory_chunk(ss, dat)
