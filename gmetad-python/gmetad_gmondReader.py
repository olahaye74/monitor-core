#/*******************************************************************************
#* Portions Copyright (C) 2008 Novell, Inc. All rights reserved.
#*
#* Redistribution and use in source and binary forms, with or without
#* modification, are permitted provided that the following conditions are met:
#*
#*  - Redistributions of source code must retain the above copyright notice,
#*    this list of conditions and the following disclaimer.
#*
#*  - Redistributions in binary form must reproduce the above copyright notice,
#*    this list of conditions and the following disclaimer in the documentation
#*    and/or other materials provided with the distribution.
#*
#*  - Neither the name of Novell, Inc. nor the names of its
#*    contributors may be used to endorse or promote products derived from this
#*    software without specific prior written permission.
#*
#* THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS ``AS IS''
#* AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
#* IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
#* ARE DISCLAIMED. IN NO EVENT SHALL Novell, Inc. OR THE CONTRIBUTORS
#* BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
#* CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
#* SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
#* INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
#* CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
#* ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
#* POSSIBILITY OF SUCH DAMAGE.
#*
#* Authors: Matt Ryan (mrayn novell.com)
#*                  Brad Nicholes (bnicholes novell.com)
#******************************************************************************/

import threading
import xml.sax
import socket
import time
import logging

from gmetad_config import GmetadConfig, getConfig
from gmetad_random import getRandomInterval
from gmetad_data import DataStore
from gmetad_data import Element

class GmondContentHandler(xml.sax.ContentHandler):
    def __init__(self):
        xml.sax.ContentHandler.__init__(self)
        self._elemStack = []
        self._elemStackLen = 0
        self._ancestry = []
        
    def startElement(self, tag, attrs):
        ds = DataStore()
        e = Element(tag, attrs)
        if 'GANGLIA_XML' == tag:
            ds.acquireLock(self)
            self._elemStack.append(ds.getNode()) # Fetch the root node.  It has already been set into the tree.
            self._elemStackLen += 1
            cfg = getConfig()
            # We'll go ahead and update any existing GRID tag with a new one (new time) even if one already exists.
            e = Element('GRID', {'NAME':cfg[GmetadConfig.GRIDNAME], 'AUTHORITY':cfg[GmetadConfig.AUTHORITY], 'LOCALTIME':'%d' % time.time()})
            self._ancestry.append('GANGLIA_XML')
        self._elemStack.append(ds.setNode(e, self._elemStack[self._elemStackLen-1]))
        if (self._ancestry[len(self._ancestry)-1].startswith('CLUSTER') == False):
            self._ancestry.append('%s:%s'%(e.id,e.name))
        self._elemStackLen += 1
        
    def endElement(self, tag):
        if tag == 'GANGLIA_XML':
            DataStore().releaseLock(self)
        self._elemStack.pop()
        self._elemStackLen -= 1
        
    def getClusterAncestry(self):
        return self._ancestry

class GmondReader(threading.Thread):
    def __init__(self,dataSource,name=None,target=None,args=(),kwargs={}):
        threading.Thread.__init__(self,name,target,args,kwargs)
        self._cond = threading.Condition()
        self._shuttingDown = False
        self.dataSource = dataSource
        self.lastKnownGoodHost = 0
        logging.debug('Reader created for cluster %s' % self.dataSource.name)
        
    def _getEndpoint(self, hostspec, port=8649):
        hostinfo = hostspec.split(':')
        if len(hostinfo) > 1:
            port = int(hostinfo[1])
        return (hostinfo[0], port)
        
    def run(self):
        while not self._shuttingDown:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.connect( self._getEndpoint(self.dataSource.hosts[self.lastKnownGoodHost]) )
            except socket.error:
                curidx = self.lastKnownGoodHost
                connected=False
                while True:
                    curidx += 1
                    if curidx >= len(self.dataSource.hosts):
                        curidx = 0
                    if curidx == self.lastKnownGoodHost: break
                    try:
                        sock.connect( self._getEndpoint(self.dataSource.hosts[curidx]) )
                        self.lastKnownGoodHost = curidx
                        connected=True
                        break
                    except socket.error:
                        pass
                if not connected:
                    logging.error('Could not connect to any host for data source %s' % self.dataSource.name)
                    return
            logging.info('Quering data source %s via host %s' % (self.dataSource.name, self.dataSource.hosts[self.lastKnownGoodHost]))
            xmlbuf = ''
            while True:
                buf = sock.recv(8192)
                if not buf:
                    break
                xmlbuf += buf
            sock.close()
            if self._shuttingDown:
                break
            gch = GmondContentHandler()
            xml.sax.parseString(xmlbuf, gch)
            DataStore().updateFinished(gch.getClusterAncestry())
            self._cond.acquire()
            self._cond.wait(getRandomInterval(self.dataSource.interval))
            self._cond.release()        
            
    def shutdown(self):
        self._shuttingDown = True
        self._cond.acquire()
        self._cond.notifyAll()
        self._cond.release()
        self.join()