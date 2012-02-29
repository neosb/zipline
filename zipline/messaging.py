"""
Commonly used messaging components.
"""
import json
import uuid
import datetime
import zipline.protocol as zp
import zipline.util as qutil
from zipline.component import Component

from zipline.protocol import CONTROL_PROTOCOL

class ComponentHost(Component):
    """
    Components that can launch multiple sub-components, synchronize their
    start, and then wait for all components to be finished.
    """

    def __init__(self, addresses, gevent_needed=False):
        Component.__init__(self)
        self.addresses = addresses

        self.components     = {}
        self.sync_register  = {}
        self.timeout        = datetime.timedelta(seconds=5)
        self.gevent_needed  = gevent_needed
        self.heartbeat_timeout = 2000

        self.feed           = ParallelBuffer()
        self.merge          = MergedParallelBuffer()
        self.passthrough    = PassthroughTransform()
        self.controller     = None

        #register the feed and the merge
        self.register_components([self.feed, self.merge, self.passthrough])

    def register_controller(self, controller):
        self.controller = controller

        for component in self.components.itervalues():
            component.controller = controller

    def register_components(self, components):
        for component in components:
            component.gevent_needed = self.gevent_needed
            component.addresses = self.addresses

            if self.controller:
                component.controller = self.controller

            self.components[component.get_id] = component
            self.sync_register[component.get_id] = datetime.datetime.utcnow()

            if(isinstance(component, DataSource)):
                self.feed.add_source(component.get_id)
            if(isinstance(component, BaseTransform)):
                self.merge.add_source(component.get_id)

    def unregister_component(self, component_id):
        del self.components[component_id]
        del self.sync_register[component_id]

    def setup_sync(self):
        """
        """
        qutil.LOGGER.debug("Connecting sync server.")

        self.sync_socket = self.context.socket(self.zmq.REP)
        self.sync_socket.bind(self.addresses['sync_address'])

        # There is a namespace collision between three classes
        # which use the self.poller property to mean different
        # things.
        # =====================================================
        self.sync_poller = self.zmq.Poller()
        self.sync_poller.register(self.sync_socket, self.zmq.POLLIN)
        # =====================================================

        self.sockets.append(self.sync_socket)

    def open(self):
        for component in self.components.values():
            self.launch_component(component)
        self.launch_controller()

    def is_timed_out(self):
        cur_time = datetime.datetime.utcnow()

        if len(self.components) == 0:
            qutil.LOGGER.info("Component register is empty.")
            return True

        for source, last_dt in self.sync_register.iteritems():
            if (cur_time - last_dt) > self.timeout:
                qutil.LOGGER.info(
                    "Time out for {source}. Current component registery: {reg}".
                    format(source=source, reg=self.components)
                )
                return True

        return False

    def loop(self):

        while not self.is_timed_out():
            # wait for synchronization request
            socks = dict(self.sync_poller.poll(self.heartbeat_timeout)) #timeout after 2 seconds.

            if self.sync_socket in socks and socks[self.sync_socket] == self.zmq.POLLIN:
                msg = self.sync_socket.recv()
                parts = msg.split(':')

                if len(parts) != 2:
                    qutil.LOGGER.info("got bad confirm: {msg}".format(msg=msg))
                    continue

                sync_id, status = parts

                if status == str(CONTROL_PROTOCOL.DONE): # TODO: other way around
                    qutil.LOGGER.info("{id} is DONE".format(id=sync_id))
                    self.unregister_component(sync_id)
                else:
                    self.sync_register[sync_id] = datetime.datetime.utcnow()

                #qutil.LOGGER.info("confirmed {id}".format(id=msg))
                # send synchronization reply
                self.sync_socket.send('ack', self.zmq.NOBLOCK)

    def launch_controller(self, controller):
        raise NotImplementedError

    def launch_component(self, component):
        raise NotImplementedError


class SimulatorBase(ComponentHost):
    """
    Simulator coordinates the launch and communication of source, feed, transform, and merge components.
    """

    def __init__(self, addresses, gevent_needed=False):
        """
        """
        ComponentHost.__init__(self, addresses, gevent_needed)

    def simulate(self):
        self.run()

    def get_id(self):
        return "Simulator"


class ParallelBuffer(Component):
    """
    Connects to N PULL sockets, publishing all messages received to a PUB
    socket.  Published messages are guaranteed to be in chronological order
    based on message property dt.  Expects to be instantiated in one execution
    context (thread, process, etc) and run in another.
    """

    def __init__(self):
        Component.__init__(self)
        self.sent_count             = 0
        self.received_count         = 0
        self.draining               = False
        #data source component ID -> List of messages
        self.data_buffer            = {}
        self.ds_finished_counter    = 0


    @property
    def get_id(self):
        return "FEED"

    def add_source(self, source_id):
        self.data_buffer[source_id] = []

    def open(self):
        self.pull_socket = self.bind_data()
        self.feed_socket = self.bind_feed()

    def do_work(self):
        # wait for synchronization reply from the host
        socks = dict(self.poll.poll(self.heartbeat_timeout)) #timeout after 2 seconds.

        if self.pull_socket in socks and socks[self.pull_socket] == self.zmq.POLLIN:
            message = self.pull_socket.recv()
            if message == str(CONTROL_PROTOCOL.DONE):
                self.ds_finished_counter += 1
                if len(self.data_buffer) == self.ds_finished_counter:
                     #drain any remaining messages in the buffer
                    self.drain()
                    self.signal_done()
            else:
                event = self.unframe(message)
                self.append(event)
                self.send_next()
                
    def __len__(self):
        """
        Buffer's length is same as internal map holding separate
        sorted arrays of events keyed by source id.
        """
        return len(self.data_buffer)

    def append(self, event):
        """
        Add an event to the buffer for the source specified by
        source_id.
        """
        self.data_buffer[event.source_id].append(event)
        self.received_count += 1

    def next(self):
        """
        Get the next message in chronological order.
        """
        if not(self.is_full() or self.draining):
            return

        cur = None
        earliest = None
        for events in self.data_buffer.values():
            if len(events) == 0:
                continue
            cur = events
            if (earliest == None) or (cur[0].dt <= earliest[0].dt):
                earliest = cur

        if earliest != None:
            return earliest.pop(0)

    def is_full(self):
        """
        Indicates whether the buffer has messages in buffer for
        all un-DONE sources.
        """
        for events in self.data_buffer.values():
            if len(events) == 0:
                return False
        return True

    def pending_messages(self):
        """
        Returns the count of all events from all sources in the
        buffer.
        """
        total = 0
        for events in self.data_buffer.values():
            total += len(events)
        return total

    def drain(self):
        """
        Send all messages in the buffer
        """
        self.draining = True
        while(self.pending_messages() > 0):
            self.send_next()

    def send_next(self):
        """
        Send the (chronologically) next message in the buffer.
        """
        if(not(self.is_full() or self.draining)):
            return

        event = self.next()
        if(event != None):
            self.feed_socket.send(self.frame(event), self.zmq.NOBLOCK)
            self.sent_count += 1

    def unframe(self, msg):
        return zp.DATASOURCE_UNFRAME(msg)
        
    def frame(self, event):
        return zp.FEED_FRAME(event)


class MergedParallelBuffer(ParallelBuffer):
    """
    Merges multiple streams of events into single messages.
    """

    def __init__(self):
        ParallelBuffer.__init__(self)

    def open(self):
        self.pull_socket = self.bind_merge()
        self.feed_socket = self.bind_result()

    def next(self):
        """Get the next merged message from the feed buffer."""
        if(not(self.is_full() or self.draining)):
            return

        #get the raw event from the passthrough transform.
        result = self.data_buffer["PASSTHROUGH"].pop(0).PASSTHROUGH
        for source, events in self.data_buffer.iteritems():
            if source == "PASSTHROUGH":
                continue
            if len(events) > 0:
                cur = events.pop(0)
                result.merge(cur)
        return result

    @property
    def get_id(self):
        return "MERGE"
        
    def unframe(self, msg):
        return zp.TRANSFORM_UNFRAME(msg)
        
    def frame(self, event):
        return zp.MERGE_FRAME(event)
        
    #
    def append(self, event):
        """
        :param event: a namedict with one entry. key is the name of the transform, value is the transformed value.
        Add an event to the buffer for the source specified by
        source_id.
        """
        
        self.data_buffer[event.__dict__.keys()[0]].append(event)
        self.received_count += 1


class BaseTransform(Component):
    """Top level execution entry point for the transform::

            - connects to the feed socket to subscribe to events
            - connets to the result socket (most oftened bound by a TransformsMerge) to PUSH transforms
            - processes all messages received from feed, until DONE message received
            - pushes all transforms
            - sends DONE to result socket, closes all sockets and context

    Parent class for feed transforms. Subclass and override transform
    method to create a new derived value from the combined feed."""

    def __init__(self, name):
        Component.__init__(self)
        self.state         = {}
        self.state['name'] = name

    @property
    def get_id(self):
        return self.state['name']

    def open(self):
        """
        Establishes zmq connections.
        """
        #create the feed.
        self.feed_socket = self.connect_feed()
        #create the result PUSH
        self.result_socket = self.connect_merge()

    def do_work(self):
        """
        Loops until feed's DONE message is received:
            - receive an event from the data feed
            - call transform (subclass' method) on event
            - send the transformed event
        """
        socks = dict(self.poll.poll(self.heartbeat_timeout)) #timeout after 2 seconds.
        if self.feed_socket in socks and socks[self.feed_socket] == self.zmq.POLLIN:
            message = self.feed_socket.recv()
            if message == str(CONTROL_PROTOCOL.DONE):
                self.signal_done()
                return

            event = zp.FEED_UNFRAME(message)
            cur_state = self.transform(event)
            qutil.LOGGER.info("state of transform is: {state}".format(state=cur_state))
            self.result_socket.send(zp.TRANSFORM_FRAME(cur_state['name'], cur_state['value']), self.zmq.NOBLOCK)

    def transform(self, event):
        """
        Must return the transformed value as a map with::

            {name:"name of new transform", value: "value of new field"}

        Transforms run in parallel and results are merged into a single map, so
        transform names must be unique.  Best practice is to use the self.state
        object initialized from the transform configuration, and only set the
        transformed value::

            self.state['value'] = transformed_value
        """
        raise NotImplementedError


class PassthroughTransform(BaseTransform):

    def __init__(self):
        BaseTransform.__init__(self, "PASSTHROUGH")

    def do_work(self):
        """
        Loops until feed's DONE message is received:
            - receive an event from the data feed
            - call transform (subclass' method) on event
            - send the transformed event
        """
        socks = dict(self.poll.poll(self.heartbeat_timeout)) #timeout after 2 seconds.
        if self.feed_socket in socks and socks[self.feed_socket] == self.zmq.POLLIN:
            message = self.feed_socket.recv()
            if message == str(CONTROL_PROTOCOL.DONE):
                self.signal_done()
                return
        #message is already FEED_FRAMEd, send it as the value.
        self.result_socket.send(zp.TRANSFORM_FRAME("PASSTHROUGH", message), self.zmq.NOBLOCK)


class DataSource(Component):
    """
    Baseclass for data sources. Subclass and implement send_all - usually this
    means looping through all records in a store, converting to a dict, and
    calling send(map).
    """
    def __init__(self, source_id, *args):
        Component.__init__(self)
        self.source_id        = source_id
        self.cur_event = None
        self.init_ds(args)
        
    def init_ds(*args):
        pass

    @property
    def get_id(self):
        return self.source_id

    def open(self):
        #create the data sink. Based on http://zguide.zeromq.org/py:tasksink2
        self.data_socket = self.connect_data()

    def get_type(self):
        raise NotImplementedError

    
    
        




