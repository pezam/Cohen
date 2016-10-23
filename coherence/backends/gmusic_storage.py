# -*- coding: utf-8 -*-

# Licensed under the MIT license
# http://opensource.org/licenses/mit-license.php

# Copyright 2016, Jan Lukas Deichmann <lukasdeichmann@gmail.com>

"""
This is a Media Backend that allows you to access your Google Play Music Library.
"""

from coherence import log
from coherence.backend import BackendStore
from coherence.backend import BackendItem
from coherence.upnp.core import DIDLLite
from coherence.upnp.core import utils
from twisted.internet import reactor, task
from twisted.web import server
from gmusicapi import Mobileclient

import tempfile
import traceback
import sys
import os

# Define global identifiers as well as the cache
cache = {}

recent_cache = {}
ROOT_ID = 0
TRACKS_ID = 10
ALBUM_ID = 20
ARTIST_ID = 30
PLAYLIST_ID = 40


class GmusicTrack(BackendItem):
    def __init__(self, parent_id, store, id, title, artist, album, artist_id, album_id, track_no, duration, albumArtURI):
        BackendItem.__init__(self)
        self.parent_id = parent_id
        self.update_id = 0

        self.id = id
        self.title = title
        self.album = album
        self.artist_id = artist_id
        self.album_id = album_id
        self.store = store
        self.duration = duration
        self.albumArtURI = albumArtURI
        self.artist = artist
        self.track_no = track_no
        self.name = self.title

        self.mimetype = "audio/mpeg"

        self.item = DIDLLite.MusicTrack(id, parent_id, self.title)
        self.item.albumArtURI = self.albumArtURI
        self.item.artist = self.artist
        self.item.originalTrackNumber = track_no

        x = int(self.duration) / 1000
        seconds = x % 60.0
        x /= 60
        minutes = x % 60
        x /= 60
        hours = x % 24

        self.item.duration = ("%02d:%02d:%02.3f") % (hours, minutes, seconds)


        self.url = self.store.urlbase + str(self.id)

        res = DIDLLite.Resource(self.url, 'http-get:*:%s:*' % self.mimetype)
        res.size = 0
        res.duration = self.item.duration
        self.item.res.append(res)

        self.location = GmusicProxyStream(self, self.store, self.id)


class GmusicAlbum(BackendItem):
    def __init__(self, parent_id, store, id, title, artist_id, artist, albumArtURI):
        BackendItem.__init__(self)
        self.parent_id = parent_id
        self.update_id = 0

        self.id = id
        self.title = title
        self.artist = artist
        self.artist_id = artist_id
        self.store = store
        self.albumArtURI = albumArtURI
        self.name = self.title

        self.tracks = {}

        self.mimetype = "directory"


    def get_children(self, start=0, end=0):
#        print([value for (key, value) in sorted(self.tracks.items())])
        return [value for (key, value) in sorted(self.tracks.items())]

    def get_child_count(self):
        return len(self.tracks)

    def get_item(self, parent_id=ALBUM_ID):
        item = DIDLLite.MusicAlbum(self.id, parent_id, self.title)
        item.childCount = self.get_child_count()
        item.artist = self.artist
        item.albumArtURI = self.albumArtURI
        return item

class GmusicPlaylist(BackendItem):
    def __init__(self, parent_id, store, id, name, author, author_image):
        BackendItem.__init__(self)
        self.parent_id = parent_id
        self.update_id = 0
        self.store = store

        self.id = id
        self.name = name
        self.author = author
        self.author_image = author_image

        self.tracks = []

        self.mimetype = "directory"

    def get_children(self, start=0, end=0):
        return self.tracks

    def get_child_count(self):
        return len(self.tracks)

    def get_item(self, parent_id=PLAYLIST_ID):
        item = DIDLLite.MusicAlbum(self.id, parent_id, self.name)
        item.childCount = self.get_child_count()
        item.artist = self.author
        item.albumArtURI = self.author_image
        return item

class GmusicContainer(BackendItem):
    def __init__(self, parent_id, id, name):
        BackendItem.__init__(self)
        self.parent_id = parent_id
        self.id = id

        self.name = name
        self.mimetype = "directory"

        self.update_id = 0
        self.children = []
        self.item = DIDLLite.Container(id, parent_id, self.name)

        self.item.childCount = None  # will be set as soon as we have images

    def get_children(self, start=0, end=0):
        if end != 0:
            return self.children[start:end]
        return self.children[start:]

    def get_child_count(self):
        return len(self.children)

    def get_item(self):
        return self.item

    def get_name(self):
        return self.name

    def get_id(self):
        return self.id


class GmusicProxyStream(utils.ReverseProxyResource, log.Loggable):
    logCategory = 'gmusic_stream'

    def __init__(self, parent, store, id):
        log.Loggable.__init__(self)
        self.id = id
        self.parent = parent
        self.store = store
        self.debug("ProxyStream init")

    def requestFinished(self, result):
        """ self.connection is set in utils.ReverseProxyResource.render """
        self.info("ProxyStream requestFinished: %s", result)
        if hasattr(self, 'connection'):
            self.connection.transport.loseConnection()

    def gotDownloadError(self, error, request):
        self.info("Unable to download stream to file: %s", self.stream_url)
        self.info(request)
        self.info(error)

    def gotFile(self, result, (request, tmpfile, filename)):
        downloadedFile = utils.StaticFile(filename, self.parent.mimetype)
        downloadedFile.type = self.parent.mimetype
        self.filesize = downloadedFile.getFileSize()
        self.mimetype = self.parent.mimetype
        downloadedFile.encoding = None
        self.info("File downloaded")
        cache[self.id] = (tmpfile, filename)
        # mark as recent
        recent_cache[self.id] = cache[self.id]
        file = downloadedFile.render(request)
        self.info("File rendered")
        if isinstance(file, int):
            return file
        request.write(file)
        request.finish()

    def getFile(self, request):

        if not self.id in cache:
            real_url = self.store.api.get_stream_url(self.id)

            (tmpfile, tmpfilename) = tempfile.mkstemp()
            res = utils.downloadPage(real_url, tmpfilename, supportPartial=1)
            res.addCallback(self.gotFile, (request, tmpfile, tmpfilename))
            res.addErrback(self.gotDownloadError, request)
            self.info("Started download")
            return server.NOT_DONE_YET

        else:
            downloadedFile = utils.StaticFile(cache[self.id][1], self.parent.mimetype)
            # mark as recent
            recent_cache[self.id] = cache[self.id]
            downloadedFile.type = self.parent.mimetype
            self.filesize = downloadedFile.getFileSize()
            self.parent.item.size = self.filesize
            self.mimetype = self.parent.mimetype
            downloadedFile.encoding = None
            self.info("File downloaded")
            file = downloadedFile.render(request)
            self.info("File rendered")
            if isinstance(file, int):
                return file
            request.write(file)
            request.finish()

    def render(self, request):
        self.debug("render %r", request)

        d = request.notifyFinish()
        d.addBoth(self.requestFinished)

        self.getFile(request)

        self.info("Request: %s %s", self.parent.id, request)

        return server.NOT_DONE_YET


########## The server
# As already said before the implementation of the server is done in an
# inheritance of a BackendStore. This is where the real code happens (usually).
# In our case this would be: downloading the page, parsing the content, saving
# it in the models and returning them on request.

class GmusicStore(BackendStore):

    # this *must* be set. Because the (most used) MediaServer Coherence also
    # allows other kind of Backends (like remote lights).
    implements = ['MediaServer']

    def __init__(self, server, *args, **kwargs):
        # first we initialize our heritage
        BackendStore.__init__(self, server, **kwargs)

        reload(sys)
        sys.setdefaultencoding('utf-8')

        # When a Backend is initialized, the configuration is given as keyword
        # arguments to the initialization. We receive it here as a dictionary
        # and allow some values to be set:

        # the name of the MediaServer as it appears in the network
        self.name = kwargs.get('name', 'Google Music')

        # timeout between updates in hours:
        self.refresh = int(kwargs.get('refresh', 1)) * (60 * 60)

        # the UPnP device that's hosting that backend, that's already done
        # in the BackendStore.__init__, just left here the sake of completeness
        self.server = server

        # initialize our containers
        self.container = GmusicContainer(None, ROOT_ID, "Google Music")
        self.container.tracks = GmusicContainer(self.container, TRACKS_ID, "Tracks")
        self.container.albums = GmusicContainer(self.container, ALBUM_ID, "Albums")
        self.container.playlists = GmusicContainer(self.container, PLAYLIST_ID, "Playlists")

        self.container.children.append(self.container.tracks)
        self.container.children.append(self.container.albums)
        self.container.children.append(self.container.playlists)

        # but as we also have to return them on 'get_by_id', we have our local
        # store of items per id:
        self.tracks = {}
        self.albums = {}
        self.artists = {}
        self.playlists = {}

        # we tell that if an XBox sends a request for images we'll
        # map the WMC id of that request to our local one
        # Todo: What the hell is going on here?
        # self.wmc_mapping = {'16': 0}

        self.username = kwargs.get('username', '')
        self.password = kwargs.get('password', '')
        self.device_id = kwargs.get('device_id', '')

        self.api = Mobileclient()

        if not self.login():
            self.info("Could not login")
            return

        # and trigger an update of the data
        dfr = self.update_data()

        # So, even though the initialize is kind of done, Coherence does not yet
        # announce our Media Server.
        # Coherence does wait for signal send by us that we are ready now.
        # And we don't want that to happen as long as we don't have succeeded
        # in fetching some first data, so we delay this signaling after the update is done:
        dfr.addCallback(self.init_completed)
        dfr.addCallback(self.queue_update)

    def login(self):
        return self.api.login(self.username, self.password, self.device_id)

    def get_by_id(self, id):
        print("asked for", id, type(id))
        # what ever we are asked for, we want to return the container only
        if isinstance(id, basestring):
            id = id.split('@', 1)
            id = id[0]
        if id == str(ROOT_ID):
            return self.container
        if id == str(ALBUM_ID):
            return self.container.albums
        if id == str(TRACKS_ID):
            return self.container.tracks
        if id == str(PLAYLIST_ID):
            return self.container.playlists

        if id in self.albums:
            self.info("id in albums:", id)
            album = self.albums.get(id, None)
            return album

        if id in self.playlists:
            self.info("id in playlists:", id)
            playlist = self.playlists.get(id, None)
            return playlist

        return self.tracks.get(id, None)

    def upnp_init(self):
        # after the signal was triggered, this method is called by coherence and

        # from now on self.server is existing and we can do
        # the necessary setup here

        # that allows us to specify our server options in more detail.

        # here we define what kind of media content we do provide
        # mostly needed to make some naughty DLNA devices behave
        # will probably move into Coherence internals one day
        self.server.connection_manager_server.set_variable(0, 'SourceProtocolInfo',
                                                           ['http-get:*:audio/mpeg:*',
                                                            'http-get:*:application/ogg:*', ]
                                                           )

        # and as it was done after we fetched the data the first time
        # we want to take care about the server wide updates as well
        self._update_container()

    def _update_container(self, result=None):
        # we need to inform Coherence about these changes
        # again this is something that will probably move
        # into Coherence internals one day
        if self.server:
            self.server.content_directory_server.set_variable(0,
                    'SystemUpdateID', self.update_id)
            value = (ROOT_ID, self.container.update_id)
            self.server.content_directory_server.set_variable(0,
                    'ContainerUpdateIDs', value)
        return result

    def update_loop(self):
        # in the loop we want to call update_data
        dfr = self.update_data()
        # after it was done we want to take care of updating
        # the container
        dfr.addCallback(self._update_container)
        # in ANY case queue an update of the data
        dfr.addBoth(self.queue_update)
        # finally clean the tempfiles
        print("Cleaning tempfiles")
        for file_id, file_tmp in cache.iteritems():
            if file_id not in recent_cache:
                os.close(file_tmp[0])
                os.remove(file_tmp[1])
        cache.clear()
        cache.update(recent_cache)
        recent_cache.clear()

    def get_data(self):
        subscribed_to_playlists = [p for p in self.api.get_all_playlists() if p.get('type') == 'SHARED']
        for playlist in subscribed_to_playlists:
            playlist["tracks"] = self.api.get_shared_playlist_contents(playlist.get("shareToken", ""))
        return (self.api.get_all_songs(), self.api.get_all_user_playlist_contents(), subscribed_to_playlists)

    def update_data(self):
        # trigger an update of the data
        self.info("Updating data")
        dfr = task.deferLater(reactor, 0, self.get_data)
        # then parse the data into our models
        dfr.addCallback(self.parse_data)
        # self.parse_data(dfr)
        self.info("Finished update")
        return dfr

    def parse_library(self, songs):
        for song in songs:
            try:
                song_id = song.get("storeId", song.get("nid", 0))
                title = song.get("title", "")
                artist_id = song.get("artistId", [0])[0]
                album_id = song.get("albumId", 0)
                album_name = song.get("album", "")
                duration = song.get("durationMillis", 0)
                album_art_uri = song.get("albumArtRef", [{"url":""}])[0].get("url", "")
                track_no = song.get("trackNumber", "0")
                disc_no = song.get("discNumber", "0")
                artist = song.get("artist", "")
                album_artist = song.get("albumArtist", artist)
                if album_id in self.albums:
                    album = self.albums[album_id]
                else:
                    album = GmusicAlbum(ALBUM_ID, self, album_id, album_name, artist_id, album_artist, album_art_uri)
                    self.container.albums.children.append(album)
                    self.albums[album_id] = album

                track = GmusicTrack(TRACKS_ID, self, song_id, title, artist, album_name, artist_id, album_id, track_no,
                                    duration, album_art_uri)

                album.tracks[int(str(disc_no) + str('{:0>10}'.format(track_no)))] = track
                self.container.tracks.children.append(track)
                self.tracks[song_id] = track
                # i = i + 1
            except Exception as e:
                print(e)
                print(song)
                traceback.print_exc()
        try:
            self.container.albums.children.sort(key=lambda x: (x.artist, x.title))
        except Exception as e:
            print("Failed to sort albums")
            print(e)
            traceback.print_exc()

    def parse_playlist_tracks(self, tracks):
        tracklist = []
        for track in tracks:
            try:
                song = track.get("track", {})
                song_id = song.get("storeId", song.get("nid", 0))
                if song_id in self.tracks:
                    tracklist.append(self.tracks[song_id])
                else:
                    title = song.get("title", "")
                    artist_id = song.get("artistId", [0])[0]
                    album_id = song.get("albumId", 0)
                    album_name = song.get("album", "")
                    duration = song.get("durationMillis", 0)
                    album_art_uri = song.get("albumArtRef", [{"url":""}])[0].get("url", "")
                    track_no = song.get("trackNumber", "0")
                    artist = song.get("artist", "")

                    track_container = GmusicTrack(TRACKS_ID, self, song_id, title, artist, album_name, artist_id, album_id, track_no,
                                        duration, album_art_uri)
                    tracklist.append(track_container)
                    self.tracks[song_id] = track_container
            except Exception as e:
                print(e)
                print(track)
                traceback.print_exc()

        return tracklist

    def parse_playlists(self, playlists):
        for playlist in playlists:
            try:
                playlist_id = playlist.get("id", playlist.get("shareToken", "DEBUG: NO_ID_TOKEN"))
                playlist_owner = playlist.get("ownerName", "DEBUG: NO_AUTHOR")
                playlist_owner_image = playlist.get("ownerProfilePhotoUrl", "DEBUG: NO_AUTHOR_IMAGE")
                playlist_name = playlist.get("name", "DEBUG: NO_NAME")
                playlist_tracks = playlist.get("tracks", {})

                playlist_container = GmusicPlaylist(PLAYLIST_ID, self, playlist_id, playlist_name, playlist_owner, playlist_owner_image)
                self.container.playlists.children.append(playlist_container)
                self.playlists[playlist_id] = playlist_container

                playlist_container.tracks = self.parse_playlist_tracks(playlist_tracks)

            except Exception as e:
                print(e)
                print(playlist)
                traceback.print_exc()

    def parse_data(self, data):
        self.info("Parsing data")
        # reset the storage
        self.container.tracks.children = []
        self.container.albums.children = []
        self.container.playlists.children = []
        self.tracks = {}
        self.albums = {}
        self.playlists = {}

        self.parse_library(data[0])
        self.parse_playlists(data[1])
        self.parse_playlists(data[2])

        self.info("Finished parsing")

        # and increase the container update id and the system update id
        # so that the clients can refresh with the new data
        # Todo: Does this actually do anyting?
        self.container.update_id += 1
        self.update_id += 1

    def queue_update(self, error_or_failure):
        # We use the reactor to queue another updating of our data
        print error_or_failure
        reactor.callLater(self.refresh, self.update_loop)


if __name__ == '__main__':

    from coherence.base import Coherence

    def main():
        config = {}
        config['logmode'] = 'info'
        c = Coherence(config)
        f = c.add_plugin('GmusicStore',
                         username="",
                         password="",
                         device_id="",
                         no_thread_needed=True)

    reactor.callWhenRunning(main)
    reactor.run()
