# -*- coding: utf-8 -*-
"""FTP scanning layer, used to sweep FTP servers for data.
"""

import ftplib
import os.path
import logging
import re
from datetime import datetime


class TooDeepError(Exception):
    def __init__(self, depth, path):
        self.depth = depth
        self.path = path

    def __str__(self):
        return 'Too deep (%d levels!). Stopping at %s' % (self.depth, self.path)


class FTPScanner(object):

    DATE_FORMAT = '%Y%m%d%H%M%S'

    def __init__(self, host, ftp_class=ftplib.FTP):
        self.host = host
        self.ftp_class = ftp_class
        self.log = logging.getLogger('ftpvista.scanner.%s' % self.host.replace('.', '_'))

    def grab_info(self, facts):
        isdir, skip = False, False

        if facts['type'] in ['cdir', 'pdir']:
            isdir = True
            skip = True

        if facts['type'] == 'dir':
            isdir = True

        if isdir and ('e' not in facts['perm'] or 'l' not in facts['perm']):
            skip = True

        return isdir, not skip

    def parse_date(self, date):
        if re.match(self.DATE_WITH_YEAR_PATTERN, date):
            return datetime.strptime(date, '%b %d %Y')

        elif re.match(self.DATE_WITH_TIME_PATTERN, date):
            date = datetime.strptime(date, '%b %d %H:%M')

            # the default value for the year is 1900
            return date.replace(datetime.now().year)

        else:
            self.log.warn('Unknown date format : %r', date)
            return None

    def connect(self):
        self.ftp = self.ftp_class(self.host)
        self.ftp.login()
        self.ftp.set_pasv(True)
        self.ftp.sendcmd("OPTS UTF8 ON")
        self.ftp.encoding = 'utf-8'

    def disconnect(self):
        self.ftp.quit()

    def scan_legacy(self, parse_line):
        items = []
        files = self.ftp.nlst()
        self.ftp.retrlines('LIST', items.append)
        items = [item.split() for item in items]

        def interpret_line(line, file_name):
            import datetime
            import time
            permissions = line.pop(0)
            line.pop(0)
            line.pop(0)
            line.pop(0)
            size = line.pop(0)
            month = line.pop(0)
            month = datetime.datetime.strptime(month, "%b").month
            if month < 10:
                month = '0%d' % month
            else:
                month = str(month)
            day = line.pop(0)
            hour = line.pop(0)
            if re.match('([0-9]+)\:([0-9])', hour):
                year = str(datetime.date.today().year)
            else:
                year = hour
                hour = '00:00'
            hour = re.sub(':', '', hour)
            modify = '%s%s%s%s00' % (year, month, day, hour)
            if file_name == '.':
                typ = 'cdir'
            elif file_name == '..':
                typ = 'pdir'
            elif permissions[0] == 'd':
                typ = 'dir'
            else:
                typ = 'file'
            if typ == 'file':
                perm = 'adfrw'
            else:
                perm = 'flcdmpe'
            return (file_name, {'perm': perm, 'type': typ, 'modify': modify, 'size': size})

        i = 0
        for line in items:
            filename, facts = interpret_line(line, files[i])
            parse_line(filename, facts)
            i+=1


    def list_files(self, dir):
        extra_dirs = []
        files = []

        def parse_line(filename, facts):
            """
            facts content example

            facts['type'] : 'cdir'
            facts['perm'] : 'fle'
            facts['modify'] : '20150920172522'
            facts['size'] : '77'
            """

            is_dir, interesting = self.grab_info(facts)
            full_path = os.path.join(dir, filename)

            if interesting:
                if is_dir:
                    extra_dirs.append(full_path)
                else:
                    try:
                        date = datetime.strptime(facts['modify'].split('.', 1)[0], FTPScanner.DATE_FORMAT)
                        # Fix dates in the future
                        if date is not None and date > datetime.now():
                            date = date.replace(datetime.now().year - 1)

                        files.append((full_path, int(facts['size']), date))
                    except ValueError:
                        return

        self.ftp.cwd(dir)
        try:
            for filename, facts in self.ftp.mlsd(facts=["type", "size", "perm", "modify"]):
                parse_line(filename, facts)
        except ftplib.error_perm as e:
            if str(e).split()[0] == '501':
                self.scan_legacy(parse_line)

        return files, extra_dirs

    def _scan(self, ignores, max_depth):
        ignores = ignores or []
        ignores = set(ignores)

        self.connect()

        dirs = set([('/', 0)])          # (path, depth) we need to process
        visited = set()                 # paths already processed
        files = []

        while len(dirs) > 0:
            try:
                cwd, depth = dirs.pop()

                if cwd in ignores:
                    self.log.info('Skipping %s' % cwd)
                    continue

                if cwd in visited:
                    self.log.warn('Loop detected, %s was already visited' % cwd)
                    continue

                cwd_files, cwd_dirs = self.list_files(cwd)

                if depth < max_depth:
                    for d in cwd_dirs:
                        dirs.add((d, depth+1))
                else:
                    self.log.error('The path is too deep (%d levels)' % depth)
                    self.log.error('Stopping at %s' % cwd)
                    self.log.error('Probably an ill-configured server.')
                    raise TooDeepError(depth, cwd)

                files.extend(cwd_files)
                visited.add(cwd)

                self.log.debug('%d directories left to scan' % len(dirs))
            except ftplib.all_errors as e:
                self.log.error("Okay, got an exception here.")
                self.log.error("Trying to reconnect and continue.")
                self.log.error("Error: %s" % e)
                self.log.error(e.__class__)
                self.connect()

        self.disconnect()
        return files

    def scan(self, ignores=None, max_depth=50):
        self.log.info('Starting FTP scan.')
        try:
            files = self._scan(ignores, max_depth)
            self.log.info('Scan complete.')
            return files
        except ftplib.error_perm as e:
            self.log.error('Permission error during scan, probably couldn\'t log in.')
            self.log.error('Error was: %s' % e)
            self.log.error('Scan terminated.')
        except ftplib.all_errors as e:
            self.log.error('Error while scanning FTP: %s' % e)
            self.log.error('Scan terminated.')
