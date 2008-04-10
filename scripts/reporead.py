#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
reporead.py

Parses a repo.db.tar.gz file and updates the Arch database with the relevant
changes.

Usage: reporead.py ARCH PATH
 ARCH:  architecture to update, and can be one of: i686, x86_64
 PATH:  full path to the repo.db.tar.gz file.

Example:
  reporead.py i686 /tmp/core.db.tar.gz

"""

###
### User Variables
###

# multi value blocks
REPOVARS = ['arch', 'backup', 'builddate', 'conflicts', 'csize', 
            'deltas', 'depends', 'desc', 'filename', 'files', 'force', 
            'groups', 'installdate', 'isize', 'license', 'md5sum', 
            'name', 'optdepends', 'packager', 'provides', 'reason', 
            'replaces', 'size', 'url', 'version']

###
### Imports
###

import os
import re
import sys
import gzip
import tarfile
import logging
from datetime import datetime
from django.core.management import setup_environ
# mung the sys path to get to django root dir, no matter
# where we are called from
archweb_app_path = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
os.chdir(archweb_app_path)
sys.path[0] = archweb_app_path
import settings
setup_environ(settings)
from pprint import pprint as pp
from cStringIO import StringIO
from logging import CRITICAL,ERROR,WARNING,INFO,DEBUG
from main.models import Arch, Package, PackageFile, PackageDepend, Repo

###
### Initialization
###

logging.basicConfig(
    level=WARNING,
    format='%(asctime)s -> %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    stream=sys.stderr)
logger = logging.getLogger()


###
### function and class definitions
###

class Pkg(object):
    """An interim 'container' object for holding Arch package data."""

    def __init__(self, val):
        selfdict = {}
        squash = ['arch', 'builddate', 'csize', 'desc', 'filename',
                  'installdate', 'isize', 'license', 'md5sum', 
                  'packager', 'size', 'url']
        
        selfdict['name'] = val['name'][0]
        del val['name']
        if 'url' not in val:
            val['url'] = ''
        for x in val.keys():
            if x in squash:
                if len(val[x]) == 0:
                    logger.warning("Package %s has no %s" % (selfdict['name'],x))
                selfdict[x] = ''.join(val[x])
            elif x == 'force':
                selfdict[x] = True
            elif x == 'version':
                version = val[x][0].rsplit('-')
                selfdict['ver'] = version[0]
                selfdict['rel'] = version[1]
            elif x == 'reason':
                selfdict[x] = int(val[x][0])
            else:
                selfdict[x] = val[x]
        self.__dict__ = selfdict
    
    def __getattr__(self,name):
        if name == 'force':
            return False
        else:
            return None


def usage():
    """Print the usage of this application."""
    print __doc__.strip()


def fetchiter_dict(cursor):
    """
    Given a DB API 2.0 cursor object that has been executed, returns a 
    dictionary that maps each field to a column index
    """
    rows = cursor.fetchmany(size=30)
    while rows:
        for row in rows:
            #pp(rows)
            #for row in rows:
            yield dictize(cursor,row)
        rows = cursor.fetchmany(size=30)


def fetchone_dict(cursor):
    """
    Given a DB API 2.0 cursor object that has been executed, returns a 
    dictionary that maps each field to a column index
    """
    results = {}
    row = cursor.fetchone()
    return dictize(cursor,row)


def dictize(cursor,row):
    result = {}
    for column,desc in enumerate(cursor.description):
        result[desc[0]] = row[column]
    return result


def db_update(archname, pkgs):
    """
    Parses a list and updates the Arch dev database accordingly.

    Arguments:
      pkgs -- A list of Pkg objects.
    
    """
    logger.info('Updating Arch: %s' % archname)
    repository = Repo.objects.get(name__iexact=pkgs[0].repo)
    architecture = Arch.objects.get(name__iexact=archname)
    dbpkgs = Package.objects.filter(arch=architecture, repo=repository)
    now = datetime.now()

    # go go set theory!
    # thank you python for having a set class <3
    dbset = set([pkg.pkgname for pkg in dbpkgs])
    syncset = set([pkg.name for pkg in pkgs])
    
    # packages in syncdb and not in database (add to database)
    in_sync_not_db = syncset - dbset
    for p in [x for x in pkgs if x.name in in_sync_not_db]:
        logger.debug("Adding package %s", p.name)
        ## note: maintainer is being set to orphan for now
        ## maybe later we can add logic to match pkgbuild maintainers 
        ## to db maintainer ids
        pkg = Package(
            repo = repository, arch=architecture, maintainer_id = 0,
            needupdate = False, url = p.url, last_update = now,
            pkgname = p.name, pkgver = p.ver, pkgrel = p.rel, 
            pkgdesc = p.desc)
        pkg.save()
        # files are not in the repo.db.tar.gz
        #for x in p.files:
        #    pkg.packagefile_set.create(path=x)
        if 'depends' in p.__dict__:
            for y in p.depends:
                # make sure we aren't adding self depends..
                # yes *sigh* i have seen them in pkgbuilds
                dpname,dpvcmp = re.match(r"([a-z0-9-]+)(.*)", y).groups()
                if dpname == p.name:
                    logger.warning('Package %s has a depend on itself' % p.name)
                    continue
                pkg.packagedepend_set.create(depname=dpname, depvcmp=dpvcmp)
                logger.debug('Added %s as dep for pkg %s' % (dpname,p.name))

    # packages in database and not in syncdb (remove from database)
    in_db_not_sync = dbset - syncset
    for p in in_db_not_sync:
        logger.info("Removing package %s from database", p)
        Package.objects.get(
            pkgname=p, arch=architecture, repo=repository).delete()

    # packages in both database and in syncdb (update in database)
    pkg_in_both = syncset & dbset
    for p in [x for x in pkgs if x.name in pkg_in_both]:
        dbp = dbpkgs.get(pkgname=p.name)
        if ''.join((p.ver,p.rel)) == ''.join((dbp.pkgver,dbp.pkgrel)):
            continue
        logger.info("Updating package %s in database", p.name)
        pkg = Package.objects.get(
            pkgname=p.name,arch=architecture, repo=repository)
        pkg.pkgver = p.ver
        pkg.pkgrel = p.rel
        pkg.pkgdesc = p.desc
        pkg.needupdate = False
        pkg.last_update = now
        pkg.save()
       
        # files are not in the repo.db.tar.gz
        #pkg.packagefile_set.all().delete()
        #for x in p.files:
        #    pkg.packagefile_set.create(path=x)
        pkg.packagedepend_set.all().delete()
        if 'depends' in p.__dict__:
            for y in p.depends:
                dpname,dpvcmp = re.match(r"([a-z0-9-]+)(.*)", y).groups()
                pkg.packagedepend_set.create(depname=dpname, depvcmp=dpvcmp)
    logger.info('Finished updating Arch: %s' % archname)


def parse_inf(iofile):
    """
    Parses an Arch repo db information file, and returns variables as a list.

    Arguments:
     iofile -- A StringIO, FileType, or other object with readlines method.

    """
    store = {}
    lines = iofile.readlines()
    blockname = None
    max = len(lines)
    i = 0
    while i < max:
        line = lines[i].strip()
        if len(line) > 0 and line[0] == '%' and line[1:-1].lower() in REPOVARS:
            blockname = line[1:-1].lower()
            logger.debug("Parsing package block %s",blockname)
            store[blockname] = []
            i += 1
            while i < max and len(lines[i].strip()) > 0:
                store[blockname].append(lines[i].strip())
                i += 1
            # here is where i would convert arrays to strings
            # based on count and type, but i dont think it is needed now
        i += 1
    return store


def parse_repo(repopath):
    """
    Parses an Arch repo db file, and returns a list of Pkg objects.

    Arguments:
     repopath -- The path of a repository db file.

    """
    logger.info("Starting repo parsing")
    if not os.path.exists(repopath):
        logger.error("Could not read file %s", repopath)
    
    logger.info("Reading repo tarfile")
    filename = os.path.split(repopath)[1]
    rindex = filename.rindex('.db.tar.gz')
    reponame = filename[:rindex]
    
    repodb = tarfile.open(repopath,"r:gz")
    ## assuming well formed tar, with dir first then files after
    ## repo-add enforces this
    logger.debug("Starting package parsing")
    pkgs = []
    tpkg = None
    while True:
        tarinfo = repodb.next()
        if tarinfo == None or tarinfo.isdir():
            if tpkg != None:
                tpkg.reset()
                data = parse_inf(tpkg)
                p = Pkg(data)
                p.repo = reponame
                logger.debug("Done parsing package %s", p.name)
                pkgs.append(p)
            if tarinfo == None:
                break
            # set new tpkg
            tpkg = StringIO()
        if tarinfo.isreg():
            if os.path.split(tarinfo.name)[1] in ('desc','depends'):
                tpkg.write(repodb.extractfile(tarinfo).read())
                tpkg.write('\n') # just in case 
    repodb.close()
    logger.info("Finished repo parsing")
    return pkgs


def main(argv=None):
    """
    Parses repo.db.tar.gz file and returns exit status.

    Keyword Arguments:
     argv -- A list/array simulating a sys.argv (default None)
             If left empty, sys.argv is used

    """
    if argv == None:
        argv = sys.argv
    if len(argv) != 3:
        usage()
        return 0
    # check if arch is valid
    available_arches = Arch.objects.all()
    if argv[1] not in [x.name for x in available_arches]:
        usage()
        return 0
    else:
        primary_arch = argv[1]

    repo_file = os.path.normpath(argv[2])
    packages = parse_repo(repo_file)
    
    # sort packages by arch -- to handle noarch stuff
    packages_arches = {}
    for arch in available_arches:
        packages_arches[arch.name] = []
    
    for package in packages:
        if package.arch not in [x.name for x in available_arches]:
            logger.warning("Package %s has missing or invalid arch" % (package.name))
            package.arch = primary_arch
        packages_arches[package.arch].append(package)

    logger.info('Starting database updates.')
    for (arch, pkgs) in packages_arches.iteritems():
        if len(pkgs) > 0:
            db_update(arch,pkgs)
    logger.info('Finished database updates.')
    return 0


###
### Main eval 
###

if __name__ == '__main__':
    logger.level = INFO
    sys.exit(main())
