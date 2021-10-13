import os
import re
import subprocess
from time import sleep
from threading import Thread, get_ident

import requests

import psycopg2
from psycopg2.errors import OperationalError
from psycopg2.extensions import register_adapter

from mapping.cube import Cube, adapt_cube
from mapping.utils import log
import config


register_adapter(Cube, adapt_cube)

MAX_THREADS = 16

# P5
# 1 1
# 255
# @

# P6
# 247 250
# 255


def process_image(filename, mime_type):

    with open(filename, "rb") as raw:
        proc = subprocess.Popen(["file", filename], stdout=subprocess.PIPE)
        tmp = proc.communicate(raw.read())
        program = None
        if tmp[0].find(b"JPEG") >= 0:
            program = "jpegtopnm"
        elif tmp[0].find(b"GIF") >= 0:
            program = "giftopnm"
        elif tmp[0].find(b"PNG") >= 0:
            program = "pngtopnm"
        else:
            print("Could not determine file type ", tmp[0])
            raise RuntimeError

        raw.seek(0)
        proc = subprocess.Popen([program, filename], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        tmp = proc.communicate(raw.read())

    proc = subprocess.Popen(["pnmscale", "-xsize", "1", "-ysize", "1"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    out = proc.communicate(tmp[0])

    lines = out[0].split(b"\n", 3)
    if lines[0].startswith(b"P6"):  # PPM
        return (lines[3][0], lines[3][1], lines[3][2])

    if lines[0].startswith(b"P5"):  # PGM
        print("graymap %d" % lines[3][0])
        return (lines[3][0], lines[3][0], lines[3][0])

    raise RuntimeError


def insert_row(release_mbid, red, green, blue, caa_id):

    # FIX THIS
    with psycopg2.connect(config.MBID_MAPPING_DATABASE_URI) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as curs:
            sql = """INSERT INTO release_color (release_mbid, red, green, blue, color, caa_id)
                          VALUES (%s, %s, %s, %s, %s::cube, %s)"""
            args = (release_mbid, red, green, blue, Cube(red, green, blue), caa_id)
            try:
                curs.execute(sql, args)
                conn.commit()
            except psycopg2.IntegrityError:
                conn.rollback()


def fetch_latest_release_mbid():

    query = """SELECT release_mbid
                 FROM release_color
             ORDER BY release_mbid DESC
                LIMIT 1"""

    with psycopg2.connect(config.MBID_MAPPING_DATABASE_URI) as mb_conn:
        with mb_conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as mb_curs:

            mb_curs.execute(query)
            while True:
                row = mb_curs.fetchone()
                if not row:
                    return None

                return row["release_mbid"]


def process_row(row):
    while True:
        headers = { 'User-Agent': 'ListenBrainz HueSound Color Bot ( rob@metabrainz.org )' }
        url = "https://beta.coverartarchive.org/release/%s/%d-250.jpg" % (row["release_mbid"], row["caa_id"])
        r = requests.get(url, headers=headers)
        if r.status_code == 200:
            if row["mime_type"] == "application/pdf":
                # TODO Skip this in the future
                print("skip PDF")
                break

            # TODO: Use proper file name
            filename = "/tmp/release-colors-%s.img" % get_ident()
            with open(filename, 'wb') as f:
                for chunk in r:
                    f.write(chunk)

            try:
                red, green, blue = process_image(filename, row["mime_type"])
                insert_row(row["release_mbid"], red, green, blue, row["caa_id"])
                print("%s %s: (%s, %s, %s)" % (get_ident(), row["release_mbid"], red, green, blue))
            except Exception as err:
                print("Could not process %s" % url)
                print(err)

            os.unlink(filename)

            break

        if r.status_code == 403:
            print("Got 403, skipping\n%s" % url)
            break
            
        if r.status_code in (503, 429):
            print("Exceeded rate limit. sleeping 2 seconds.")
            sleep(2)
            continue

        print("Unhandled %d" % r.status_code)
        break


def download_cover_art():

    log("download cover art starting...")

    latest_mbid = fetch_latest_release_mbid()
    print("latest mbid: %s" % str(latest_mbid))

    query = """SELECT caa.id AS caa_id
                    , release AS release_id
                    , release.gid AS release_mbid
                    , mime_type
                 FROM cover_art_archive.cover_art caa
                 JOIN cover_art_archive.cover_art_type cat
                   ON cat.id = caa.id
                 JOIN musicbrainz.release
                   ON caa.release = release.id
                WHERE type_id = 1 """
    args = []
    if latest_mbid:
        query += "AND release.gid > %s::UUID "
        args.append((latest_mbid,))

    query += "ORDER BY release_mbid"""

    with psycopg2.connect(config.MBID_MAPPING_DATABASE_URI) as mb_conn:
        with mb_conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as mb_curs:

            log("execute query")
            mb_curs.execute(query, tuple(args))
            log("process rows")

            threads = []
            while True:
                row = mb_curs.fetchone()
                if not row:
                    break

                while len(threads) == MAX_THREADS:
                    for i, thread in enumerate(threads):
                        if not thread.is_alive():
                            thread.join()
                            threads.pop(i)
                            break
                    else:
                        sleep(.001)
           
                t = Thread(target=process_row, args=(row,))
                t.start()
                threads.append(t)
