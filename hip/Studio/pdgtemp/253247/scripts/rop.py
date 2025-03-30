
# Copyright (c) <2020> Side Effects Software Inc.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# NAME:	        rop.py ( Python )
#
# COMMENTS:     Utility methods for working with rop nodes
#

from __future__ import print_function, absolute_import

import argparse
import errno
import glob
import os
import re
import shlex
import socket
import sys
import time
import traceback

from pdgutils import PDGNetMQRelay

try:
    import mantrafilter
    import pdgcmd
    import pdgjson
    import pdgnetrpc
except ImportError:
    import pdgjob.mantrafilter as mantrafilter
    import pdgjob.pdgcmd as pdgcmd
    import pdgjob.pdgjson as pdgjson
    import pdgjob.pdgnetrpc as pdgnetrpc

import hqueue.houdini as hq
import hou
import pdg

class RopCooker(object):
    rop_map = {
        'agentdefintioncache' : 'rop_network/OUT',
        'dopio' : 'render',
        'filecache' : 'render',
        'flipsourcecache': 'filecache1/render',
        'karma' : 'rop_usdrender',
        'rbdio::2.0' : 'filecache/render',
        'vellumdrape' : 'rop_geometry1',
        'vellumio::2.0' : 'filecache/render',
        'vellumio' : 'filecache1/render',
        'usdexport' : 'lopnet_EXPORT/USD_OUT',
    }

    post_advance_types = ['comp', 'ifd']
    mantra_parms = ['vm_picture', 'vm_dsmfilename', 'vm_dcmfilename']
    standard_parms = ['vm_picture', 'sopoutput', 'dopoutput', 'lopoutput',
        'picture', 'copoutput', 'filename', 'usdfile', 'file', 'output',
        'outputfilepath', 'outputimage', 'outfile#']

    hip_modtime = None

    # Conditionally loads a .hip file
    @classmethod
    def loadHIP(cls, work_item):
        if not work_item:
            return False

        saved_environ = os.environ.copy()

        # Get the path from the work item and check if it exists
        hip_local = work_item.fileAttribValue('hip').path
        hip_local = work_item.localizePath(hip_local)

        if not hq.fileExists(hip_local):
            work_item.addError("Cannot find file '%s'" % hip_local)
            return False

        # convert to forward slash again to handle any windows-style path
        # components that have been expanded
        hip_local = hip_local.replace('\\', '/')
        mod_time = os.path.getmtime(hip_local)

        if work_item.hasAttrib('ignoreloaderrors'):
            ignore_errors = work_item.intAttribValue('ignoreloaderrors') > 0
        else:
            ignore_errors = False

        if hou.hipFile.isNewFile() or \
            hou.hipFile.path() != hip_local or \
            cls.hip_modtime != mod_time:
            work_item.addMessage("Loading .hip file '%s'..." % hip_local)

            try:
                hou.hipFile.load(hip_local, ignore_load_warnings=True)
                cls.hip_modtime = mod_time
            except hou.OperationFailed as e:
                cls.hip_modtime = 0
                if ignore_errors:
                    work_item.addWarning(
                        "Error(s) when loading .hip file:\n{}".format(str(e)))
                else:
                    raise e

            work_item.addMessage(".hip file done loading")
        else:
            work_item.addMessage(".hip file '%s' is already loaded" % hip_local)

        # re-apply saved env vars which may have been overwritten by those saved in
        # the hip file.
        dont_set = (
            'HIP', 'HIPFILE', 'HIPNAME', 'JOB', 'PI',
            'E', 'POSE', 'status', 'EYE', 'ACTIVETAKE')

        hscript_set = hou.hscript('set -s')
        for ln in hscript_set[0].split('\n'):
            if ln.startswith('set '):
                split_val = ln[7:].split(' = ')
                if not split_val:
                    continue
                key = split_val[0]
                if (key in saved_environ) and (key not in dont_set):
                    setcmd = "setenv {} = '{}'".format(key, saved_environ[key])
                    hou.hscript(setcmd)
    
        set_hip = work_item.stringAttribValue('sethip')

        if set_hip:
            # if $ORIGINAL_HIP exists, we use that instead of the supplied
            # argument  because current working dir might be different from
            # the original hip dir
            original_hip = os.environ.get('ORIGINAL_HIP', set_hip)
            original_hip = work_item.localizePath(original_hip)

            work_item.addMessage("Resetting HIP to '%s'" % original_hip)
            hou.hscript('set HIP={}'.format(original_hip))
            hou.hscript('set HIPFILE={}/{}'.format(original_hip, hip_local))

        hou.hscript('varchange')

        return True

    # Constructs a ROP Cooker based on the node type stored on the work
    # item
    @classmethod
    def create(cls, work_item, server=None):
        if not work_item:
            return None

        node = hq.getNode(work_item.stringAttribValue('rop'))
        node_type = node.type().name()

        if node_type.startswith('baketexture::'):
            return BakeTextureCooker(work_item, server)
        elif node_type.find('sop_simple_baker') >= 0 or \
                node_type.find('rop_games_baker') >= 0:
            return GamesBakerCooker(work_item, server)
        
        return RopCooker(work_item, server)

    # Constructs a new ROP cooker instance from a work item
    def __init__(self, work_item, server=''):
        self.workItem = work_item
        self.server = server
        self.outputs = dict()

        self.hasCallback = False
        self.preFrame = None
        self.postFrame = None

        self.mantraFilters = set()

        if work_item.hasAttrib("unlocktargetrop"):
            self.unlockNode = work_item.intAttribValue("unlocktargetrop") > 0
        else:
            self.unlockNode = True

        if work_item.hasAttrib("cookinputrops"):
            self.cookInputs = work_item.intAttribValue("cookinputrops") > 0
        else:
            self.cookInputs = True

        # keep track of the original target node and the container TOP
        # node, if it's set
        target_path = work_item.stringAttribValue('rop')
        top_path = work_item.stringAttribValue('top')

        self.targetNode = hq.getNode(target_path)
        if top_path:
            self.editNode = hq.getNode(top_path)
        else:
            self.editNode = self.targetNode

        if self.editNode.isInsideLockedHDA() and self.unlockNode:
            try:
                self.editNode.allowEditingOfContents(True)
                self.workItem.addMessage(
                    "Unlocked asset that contains target node '{}'".format(
                    self.editNode.path()))
            except hou.PermissionError as e:
                self.workItem.addWarning(
                    "Unable to unlock asset for target node '{}'".format(
                    self.editNode.path()))

        self.renderNode = self.targetNode

        # find internal ROP for rendering, if needed
        type_name = self.targetNode.type().name()
        for op_name, rop_path in list(self.rop_map.items()):
            if type_name.find(op_name) >= 0:
                inner_rop = self.targetNode.node(rop_path)
                if inner_rop:
                    self.renderNode = inner_rop
                    break

        # store frame range info
        start = work_item.floatAttribValue('range', 0)
        end = work_item.floatAttribValue('range', 1)
        step = work_item.floatAttribValue('range', 2)

        if work_item.hasAttrib("real_start"):
            start = work_item.floatAttribValue('real_start')

        if work_item.hasAttrib("real_end"):
            end = work_item.floatAttribValue('real_end')

        self.frameRange = (start, end, step)

        # store render type info
        self.noRange = work_item.intAttribValue('norange') > 0
        self.singleTask = work_item.intAttribValue('singletask') > 0

        if work_item.intAttribValue('cookorder') == 0:
            self.cookOrder = hou.renderMethod.RopByRop
        else:
            self.cookOrder = hou.renderMethod.FrameByFrame

        if work_item.hasAttrib('executeparm'):
            self.executeParm = work_item.stringAttribValue('executeparm')
        else:
            self.executeParm = "execute"
    
        self.tileIndex = work_item.intAttribValue('tileindex')

        # store output parm info
        self.fileTags = work_item.stringAttribArray('filetag')
        if not self.fileTags:
            self.fileTags = [""]

        self.outputParms = work_item.stringAttribArray('outputparm')
        self.reloadParm = work_item.stringAttribValue('reloadparm')
        self.customOutputParms = \
            work_item.intAttribValue('usecustomoutputparm') > 0

        # store log parsing info
        self.logParsing = work_item.intAttribValue('logparsing')
        self.logFormat = work_item.stringAttribValue('logformat')

        # store perf mon info
        self.usePerfMon = work_item.intAttribValue('useperfmon') > 0
        self.perfMonFile = work_item.stringAttribValue('perfmonfile')

        self.debugFile = work_item.stringAttribValue('debugfile')
        self.reportDebug = work_item.intAttribValue('reportdebugoutputs') > 0

        # cache the ip of the result server to speed up rpc calls
        if not self.server:
            if not pdgcmd.disable_rpc and not work_item.isInProcess:
                hostname, port = os.environ['PDG_RESULT_SERVER'].split(':')
                self.server = socket.gethostbyname(hostname) + ':' + port
        else:
            self.server = ''

        # configure batch and dist sim settings
        self._configureBatch()

        # configure ROP specific settings
        self._configureROP()

    # Prints debug information about the nodes referenced by the cooker
    def _checkNodeInfo(self):
        node_type = self.targetNode.type()
        is_network = False

        if self.targetNode.isSubNetwork() and \
                node_type.childTypeCategory().name() == 'Driver':
            self.workItem.addMessage(
                "ROP subnet path '{}'".format(self.targetNode.path()))
            is_network = True
        else:
            self.workItem.addMessage(
                "ROP node path '{}'".format(self.targetNode.path()))

        self.workItem.addMessage(
            "ROP type name: '{}'".format(node_type.name()))
        self.workItem.addMessage(
            "ROP source path: '{}'".format(node_type.sourcePath()))

        hda_defn = node_type.definition()
        if hda_defn:
            self.workItem.addMessage("ROP library path: '{}'".format(
                hda_defn.libraryFilePath()))

        if self.targetNode != self.editNode:
            self.workItem.addMessage(
                "NOTE: Render parm edits will be applied to: '{}'".format(
                self.editNode.path()))

        if self.targetNode != self.renderNode:
            self.workItem.addMessage(
                "NOTE: Render operations will be applied to: '{}'".format(
                self.renderNode.path()))

        return is_network

    # Configures ROP-specific variables
    def _configureROP(self):
        node_type = self.targetNode.type().name()

        if node_type == 'Redshift_ROP' and 'HOUDINI_GPU_LIST' in os.environ:
            gpus = os.environ['HOUDINI_GPU_LIST'].split(",")
            gpuSettingString = ""
            for i in range(8):
                if str(i) in gpus:
                    gpuSettingString += "1"
                else:
                    gpuSettingString += "0"
            hou.hscript("Redshift_setGPU -s " + gpuSettingString)

    # Configures distributed sim and batch settings
    def _configureBatch(self):
        self.batch = self.workItem.intAttribValue('batch') > 0 
        self.dist_sim = self.workItem.intAttribValue('dist') > 0

        if self.dist_sim:
            self.batch = True

        if not self.batch:
            return

        if hasattr(self.workItem, 'batchStart'):
            self.batchStart = self.workItem.batchStart
        else:
            self.batchStart = 0
    
        self.batch_poll = self.workItem.intAttribValue('batchpoll') > 0

        if not self.dist_sim:
            return

        control_path = self.workItem.stringAttribValue('control')
        tracker_ip = self.workItem.stringAttribValue('trackerhost')
        tracker_port = self.workItem.intAttribValue('trackerport')

        slice_num = self.workItem.intAttribValue('slice')
        slice_type = self.workItem.intAttribValue('slicetype')

        slice_divs = (
            self.workItem.intAttribValue('slicedivs', 0),
            self.workItem.intAttribValue('slicedivs', 1),
            self.workItem.intAttribValue('slicedivs', 2))

        self.workItem.addMessage(
            "Sim control DOP: {}".format(control_path))
        self.workItem.addMessage(
            "Sim tracker: {}:{}".format(tracker_ip, tracker_port))
        self.workItem.addMessage(
            "Sim slice type: {} number: {} divisions: {}".format(
            slice_type, slice_num, slice_divs))

        control = hq.getNode(control_path)

        if control.isInsideLockedHDA() and self.unlockNode:
            try:
                control.allowEditingOfContents(True)
            except hou.PermissionError as e:
                self.workItem.addWarning(
                    "Unable to unlock asset for control node '{}'".format(
                    control.path()))

        try:
            control.parm('address').deleteAllKeyframes()
            control.parm('address').set(tracker_ip)
            control.parm('port').set(int(tracker_port))

            if slice_type == 0:
                pass    # nothing for particle slices as of yet
            elif slice_type == 1:
                # volume slices
                slice_div_parm = control.parmTuple("slicediv")
                vis_slice_div_parm = control.parmTuple("visslicediv")
                if slice_div_parm is not None:
                    slice_div_parm.set(slice_divs)
                if vis_slice_div_parm is not None:
                    vis_slice_div_parm.set(slice_divs)

        except hou.OperationFailed as e:
            self.workItem.addWarning(
                "Unable to set distributed sim configuration parameters")
            self.workItem.addWarning(str(e))

        hou.hscript('setenv SLICE=' + str(slice_num))
        hou.hscript('varchange')

    # Computes a batch index from a frame
    def _batchIndex(self, frame):
        return int(round((frame - self.frameRange[0])/self.frameRange[2])) + \
            self.batchStart

    # Return a list of hou.Parm for the given name.  Will have a single entry
    # unless it's a multiparm instance like outputfile_#
    def _resolveParm(self, node, parm_name):
        parms = []

        # Eval multiparm instances if a # token is found in the parm name
        if '#' in parm_name:
            i = 0
            while True:
                parm_name_x = parm_name.replace('#', str(i))
                i += 1

                parm_x = node.parm(parm_name_x)
                if not parm_x:
                    if i == 1:
                        continue
                    else:
                        break

                parms.append(parm_x)
                
        else:
            parm = node.parm(parm_name)
            if parm:
                parms.append(parm)

        return parms

    # Find all output parms on the specified node, at the specified frame
    # value.
    def _resolveParms(self, node, frame, verbose):
        output_parms = []

        if self.outputParms:
            if verbose:
                self.workItem.addMessage(
                    "Checking for output parm(s) '{}' on node '{}'".format(
                    " ".join(self.outputParms), node.path()))

            for output_parm in self.outputParms:
                parms = self._resolveParm(node, output_parm)
                if parms:
                    output_parms += parms

        if not output_parms:
            if verbose:
                self.workItem.addMessage(
                    "Checking for default output parms on node '{}'".format(
                    node.path()))

            save_ifd = node.parm('soho_outputmode')
            if save_ifd:
                if frame:
                    should_save = save_ifd.evalAtFrame(frame)
                else:
                    should_save = save_ifd.eval()

                if should_save:
                    ifd_path = node.parm('soho_diskfile')
                    if ifd_path:
                        return [ifd_path]

            for parm_name in self.standard_parms:
                output_parms = self._resolveParm(node, parm_name)
                if output_parms:
                    break

        if not output_parms:
            if verbose:
                self.workItem.addWarning(
                    "WARNING: No output parms found on node '{}'".format(
                    node.path()))
            return []

        filtered_parms = []

        for parm in output_parms:
            if parm.name() in self.mantra_parms and not self.customOutputParms:
                if node.path() in self.mantraFilters:
                    continue

                # this is a mantra non-ifd render - use the python filter
                # instead, this gives us a more accurate method of reporting
                # result artifacts
                soho_pipecmd = node.parm('soho_pipecmd')
                if soho_pipecmd:
                    if frame:
                        mantra_cmd = soho_pipecmd.evalAtFrame(frame)
                    else:
                        mantra_cmd = soho_pipecmd.eval()

                    mantra_argv = shlex.split(mantra_cmd)
                    if "-P" not in mantra_argv:
                        this_path = mantrafilter.__file__.replace('\\', '/')
                        mantra_cmd = mantra_cmd + ' -P "{}"'.format(this_path)
                        try:
                            soho_pipecmd.set(mantra_cmd)
                            self.mantraFilters.add(node.path())
                            self.workItem.addMessage(
                                "Skipping output parm '{}' for '{}' and using "
                                "Mantra frame filter to report outputs".format(
                                parm.name(), node.path()))
                        except:
                            self.workItem.addWarning(
                                "Unable to set 'soho_pipecmd' -- deep Mantra "
                                "render outputs may not appear correctly as "
                                "work item outputs")
                        continue

            filtered_parms.append(parm)

        return filtered_parms

    # Returns a list of outputs at the given frame for the node
    def _collectOutput(self, node, frame, verbose):
        outputs = []
        parms = self._resolveParms(node, frame, verbose)

        for parm in parms:
            if frame:
                outputs.append(parm.evalAtFrame(frame))
            else:
                outputs.append(parm.eval())

        return outputs

    # Collect outputs for any input ROPs that have output file paths
    def _collectOutputs(self, node, frame, verbose):
        rop_stack = [node, ]
        visited_rops = []

        outputs = []
        while len(rop_stack) > 0:
            cur_rop = rop_stack.pop()
            output = self._collectOutput(cur_rop, frame, verbose)
            if output:
                outputs.extend(output)

            visited_rops.append(cur_rop)
            for input_node in cur_rop.inputs():
                if input_node is None:
                    continue
                if input_node.type().category() == hou.ropNodeTypeCategory() \
                    and input_node not in visited_rops:
                    rop_stack.append(input_node)

        return outputs

    # Reloads parts of the scene using a custom reload parm path, if one
    # was set by the user
    def _reloadButton(self, work_item):
        if not self.reloadParm:
            return

        reload_button = hou.parm(self.reloadParm)
        if reload_button:
            try:
                reload_button.pressButton()
                work_item.addMessage('Pressed reload button "{}"'.format(
                    self.reloadParm))
            except:
                work_item.addWarning(
                    'Unable to press reload button "{}"'.format(
                    self.reloadParm))
        else:
            work_item.addWarning('Invalid reload parm path "{}"'.format(
                self.reloadParm))

    # Pre-frame callback, used for batch work items
    def _preFrame(self, time):
        frame = hou.timeToFrame(time)
        is_first = (frame == self.frameRange[0])

        # If the work item is is cooking multiple frames, store the results
        # in a list so they can be reported at the end.
        if self.singleTask:
            outputs = self._collectOutputs(
                self.targetNode, None, is_first)
            if 0 in self.outputs:
                self.outputs[0] += outputs
            else:
                self.outputs[0] = outputs
            return

        # Compute the batch index from the current frame
        batch_index = self._batchIndex(frame)
        batch_item = self.workItem.batchItems[batch_index]

        # If the batch starts when the first frame is ready, we need to poll
        # for subsequent frame dependencies
        batch_item.startSubItem(self.batch_poll)
        batch_item.addMessage('Cooking batch sub item {}'.format(batch_index))

        # Advanced the active work item
        has_advanced = self.targetNode.type().name() in self.post_advance_types

        # Apply channels and env vars for the  current sub item
        pdgjson.applyWedges(batch_item)
        pdgjson.applyEnvironment(batch_item)

        # Update time dependent attributes, or all attributes in the case
        # of COPs
        if has_advanced or self.workItem.isInProcess:
            pdg.EvaluationContext.dirtyAllAttribs()
        else:
            pdg.EvaluationContext.setTimeDependentAttribs(
                batch_item.timeDependentAttribs())

        # Press the reload parm if one was set
        self._reloadButton(batch_item)

        # Stash the result for this frame. We do this now since the result may
        # not be immediately used in the postframe if the rop uses the
        # postwrite/background writing feature. During the postwrite our active
        # work item will change since the next frame will be simulating, so we
        # need to eval the output path now and save it before that happens
        self.outputs[batch_index] = self._collectOutputs(
            self.targetNode, None, is_first)

    def _postFrame(self, time):
        # Compute the batch index from the current frame
        batch_index = self._batchIndex(hou.timeToFrame(time))
        batch_item = self.workItem.batchItems[batch_index]

        # If there's a stashed result for our index, report it to PDG. Always 
        # report sub item success in both cases.
        if batch_index in self.outputs:
            batch_item.addOutputFiles(
                self.outputs[batch_index], self.fileTags, own=True);

        batch_item.cookSubItem()

        # The Composite ROP will determine its output file before the pre-frame
        # is called, so we need to advance the work item now
        if self.targetNode.type().name() in self.post_advance_types:
            if batch_index + 1 < self.workItem.batchSize:
                batch_item = self.workItem.batchItems[batch_index+1]
                batch_item.batchParent.setActiveBatchIndex(batch_index+1)

                pdgjson.applyWedges(batch_item)
                pdgjson.applyEnvironment(batch_item)

                pdg.EvaluationContext.dirtyAllAttribs()

    # Cooks a node with a given start, end and increment
    def _render(self):
        try:
            # Always show alf progress
            alf_progress = self.editNode.parm("alfprogress")
            if alf_progress is not None:
                alf_progress.set(1)
            else:
                alf_progress = self.editNode.parm("vm_alfprogress")
                if alf_progress is not None:
                    alf_progress.set(1)

            # If the ROP Fetch is set to cook using the rop node's
            # configuration, we won't touch any of the frame range 
            # parms
            if not self.noRange:
                # Force the ROP to use the "frame range" setting, in case it
                # was saved with the "cook the current frame" setting
                valid_range = self.editNode.parm("trange")
                if valid_range is not None:
                    valid_range.set("normal")

                # If the node we're going to cook is not a ROP-like node or
                # has a custom execute button, we need to try to override the
                # frame range parm. The node will just be cooked by pressing
                # button and doesn't accept a frame range arg.
                if not hasattr(self.renderNode, 'render') or \
                        self.executeParm != "execute":
                    frame = self.editNode.parmTuple("f")
                    if frame is not None:
                        frame.deleteAllKeyframes()
                        frame.set(self.frameRange)
        except:
            self.workItem.addWarning(
                "Unable to set 'alfprogress' and/or 'trange' parameters on "
                "target ROP node '{}'".format(self.editNode.path()))

        profile = None

        # Start the perfmon if desired
        if self.usePerfMon:
            if self.perfMonFile:
                profile = hou.perfMon.startProfile("ROP Fetch Cook")
            else:
                hou.hscript("perfmon -o stdout -t ms")

        # Choose a render method based on the type of node and any custom
        # render parm overrides
        try:
            button = self.renderNode.parm(self.executeParm)
            if self.executeParm != "execute" and button:
                self.workItem.addMessage("Pressing button '{}' on node".format(
                    self.executeParm))
                button.pressButton()
            elif hasattr(self.renderNode, 'render'):
                self.workItem.addMessage("Cooking node using 'hou.Rop.render'")

                if self.noRange:
                    self.renderNode.render(
                        method=self.cookOrder,
                        ignore_inputs=not self.cookInputs)
                else:
                    self.renderNode.render(
                        self.frameRange,
                        method=self.cookOrder,
                        ignore_inputs=not self.cookInputs)
            elif button:
                self.workItem.addMessage("Pressing button 'execute' on node")
                button.pressButton()
            else:
                self.workItem.addError(
                    "Unable to cook node '{}'. No render method or "
                    "execute button was found".format(self.renderNode.path()))
                return False

            if len(self.renderNode.errors()):
                self.workItem.addError(str(self.renderNode.errors()))
                return False

        except hou.OperationFailed as e:
            self.workItem.addError(str(e))
            return False

        # If the render node and target node don't match, try to hit a
        # reload button to handle the case of an internal ROP that needs to
        # have its data reapplied to the parent node
        if self.renderNode != self.targetNode:
            try:
                self.targetNode.parm('reload').pressButton()
            except:
                pass

        if profile:
            profile.stop()

            local_path = self.workItem.localizePath(self.perfMonFile)
            self.workItem.addMessage(
                "Saving performance file to %s" % local_path)

            # Ensure file path exists
            if not os.path.exists(os.path.dirname(local_path)):
                try:
                    os.makedirs(os.path.dirname(local_path))
                except OSError as exc:
                    if exc.errno != errno.EEXIST:
                        raise

            if local_path.endswith('csv'):
                profile.exportAsCSV(local_path)
            else:
                profile.save(local_path)

            if self.reportDebug:
                self.workItem.addOutputFile(
                    self.perfMonFile, 'file/text/debug', own=True)

        return True

    def _renderEventCallbackPreframe(self, node, event_type, time):
        if event_type == self.preFrame:
            self._preFrame(time)

    def _renderEventCallbackPostframe(self, node, event_type, time):
        if event_type == self.postFrame:
            self._postFrame(time)

    def _setCallbacks(self, pre_frame, post_frame):
        try:
            self.hasCallback = True

            # Support for passing the run_before flag was added later, so
            # for backward compatibility with old Houdini version we try both
            # options here just in case.
            try:
                self.renderNode.addRenderEventCallback(
                    self._renderEventCallbackPreframe, True)
                self.renderNode.addRenderEventCallback(
                    self._renderEventCallbackPostframe, False)
            except:
                self.renderNode.addRenderEventCallback(
                    self._renderEventCallbackPreframe)
                self.renderNode.addRenderEventCallback(
                    self._renderEventCallbackPostframe)

            self.preFrame = pre_frame
            self.postFrame = post_frame
        except Exception as e:
            self.workItem.addWarning(
                "Failed to set ROP event callback on node '{}': {}".format(
                self.renderNode.path(), str(e)))

    # Cooks a single frame
    def _cookSingleFrame(self):
        is_ropnet = self._checkNodeInfo()

        # Configure output log handling
        # 
        # 0 -- always off
        # 1 -- if ROP parms exists
        # 2 -- always on
        if self.logParsing == 0:
            output_logger = None
        elif self.logParsing == 1:
            try:
                output_logger = None
                log_parm = self.targetNode.parm('pdg_logoutput')
                if log_parm:
                    output_logger = pdgcmd.StdOutReporter(self.logFormat)
                    log_parm.set(1)
            except:
                self.workItem.addWarning(
                    "Unable to set 'pdg_logoutput' on target node '{}' -- "
                    "output log parsing will be disabled.".format(
                    self.targetNode.path()))
                output_logger = None
        else:
            output_logger = pdgcmd.StdOutReporter(self.logFormat)

        try:
            fg_parm = self.editNode.parm("soho_foreground")
            if fg_parm is not None:
                fg_parm.set(1)
        except:
            self.workItem.addWarning(
                "Unable to set 'soho_foreground' parameter -- Mantra render "
                "may not perform as expected")

        try:
            vm_tile_index = self.editNode.parm("vm_tile_index")
            if vm_tile_index is not None and self.tileIndex > -1:
                # this is a mantra tiled render
                vm_tile_index.set(self.tileIndex)
        except:
            self.workItem.addWarning(
                "Unable to set 'vm_tile_index' parameter -- tiled Mantra "
                "render may not perform as expected")

        parms = []
        if not output_logger:
            parms = self._resolveParms(
                self.targetNode, self.frameRange[0], True)

        # For single task work items, we need to set a pre frame script
        # that stashs per frame output files
        if self.singleTask and parms:
            self._setCallbacks(hou.ropRenderEventType.PreFrame, None)
        else:
            self._reloadButton(self.workItem)

        if not self._render():
            return (False, [])

        results = []
        if output_logger:
            output_logger.reportAndClose(self.workItem)
        elif parms:
            if self.singleTask:
                if 0 in self.outputs:
                    result_set = set()
                    result_paths = []
                    result_tags = []
                    for i, output_file in enumerate(self.outputs[0]):
                        if output_file in result_set:
                            continue
                        result_set.update((output_file,))

                        tag = self.fileTags[i % len(self.fileTags)]
                        result_paths.append(output_file)
                        result_tags.append(tag)
                        results.append((output_file, tag))
                    
                    self.workItem.addOutputFiles(
                        result_paths, result_tags, own=True)
            else:
                output_files = self._collectOutputs(
                    self.targetNode, self.frameRange[0], True)
                output_paths = []
                output_tags = []

                for i, output_file in enumerate(output_files):
                    if i >= len(self.fileTags):
                        tag = ""
                    else:
                        tag = self.fileTags[i]

                    result = (output_file, tag)
                    output_paths.append(output_file)
                    output_tags.append(tag)
                    results.append(result)

                self.workItem.addOutputFiles(
                    output_paths, output_tags, own=True)

        elif is_ropnet:
            children = self.targetNode.allSubChildren(True, False)
            self.workItem.addMessage(
                "Checking subnet children for output parms")
            for child in children:
                output_parms = self._resolveParms(
                    child, self.frameRange[0], True)

                if output_parms:
                    output_paths = []
                    output_tags = []

                    for i, p in enumerate(output_parms):
                        if i >= len(self.fileTags):
                            tag = ""
                        else:
                            tag = self.fileTags[i]

                        result = (p.evalAtFrame(self.frameRange[0]), tag)
                        results.append(result)

                        output_paths.append(result[0])
                        output_tags.append(result[1])

                    self.workItem.addOutputFiles(
                        output_paths, output_tags, own=True)

        return (True, results)

    # Cook a batch of frames, with a batch pool call made before the frame is
    # cooked and a batch notification emitted after each frame completes
    def _cookBatchFrames(self):
        is_ropnet = self._checkNodeInfo()

        output_parms = self._resolveParms(
            self.targetNode, self.frameRange[0], True)

        if not output_parms and is_ropnet:
            self.workItem.addError(
                "When batching is enabled, the specific ROP must have an output"
                "parameter and the preframe and postframe callbacks")
            return (False, [])

        # Check if the node uses postframe or postwrite
        if self.targetNode.parm('postwrite'):
            post_frame = hou.ropRenderEventType.PostWrite
        else:
            post_frame = hou.ropRenderEventType.PostFrame

        self._setCallbacks(hou.ropRenderEventType.PreFrame, post_frame)

        if self.batchStart > 0:
            self.workItem.addMessage(
                "Report cached results for sub items 0 to {}".format(
                self.batchStart-1))

            for i in range(0, self.batchStart):
                frame = self.frameRange[0] + i*self.frameRange[2]
                batch_item = self.workItem.batchItems[i]
                batch_item.startSubItem(self.batch_poll)

                output_paths = []
                output_tags = []
                for i, output_parm in enumerate(output_parms):
                    if i >= len(self.fileTags):
                        tag = ""
                    else:
                        tag = self.fileTags[i]

                    output_paths.append(output_parm.evalAtFrame(frame))
                    output_tags.append(tag)

                batch_item.addOutputFiles(output_paths, output_tags, own=True)
                batch_item.cookSubItem()

            start_frame = self.frameRange[0] + self.batchStart*self.frameRange[2]
            self.workItem.addMessage(
                "Partial cook of batch from index={} frame={}".format(
                self.batchStart, start_frame))
            self.frameRange = \
                (start_frame, self.frameRange[1], self.frameRange[2])

        return (self._render(), [])

    # Cooks the ROP with all of the settings loaded from the work item
    def cook(self, service_mode):
        if self.batch:
            if service_mode:
                self.workItem.addError(
                    "ROP Fetch service does not support batching "
                    "distributed simulations")
                return (False, [])
            return self._cookBatchFrames()
        else:
            return self._cookSingleFrame()

    # Saves the .hip and reports it to the work item
    def saveDebugFile(self):
        if not self.debugFile:
            return

        local_path = self.workItem.localizePath(self.debugFile)
        self.workItem.addMessage("Saving debug .hip file to %s" % local_path)

        # Ensure file path exists
        if not os.path.exists(os.path.dirname(local_path)):
            try:
                os.makedirs(os.path.dirname(local_path))
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise

        hou.hipFile.save(local_path)

        if self.reportDebug:
            self.workItem.addOutputFile(
                self.debugFile, 'file/hip/debug', own=True)

    # Cleans up render event callbacks
    def cleanup(self):
        if self.hasCallback:
            try:
                self.renderNode.removeRenderEventCallback(
                    self._renderEventCallbackPreframe)
                self.renderNode.removeRenderEventCallback(
                    self._renderEventCallbackPostframe)
            except Exception as e:
                self.workItem.addWarning(
                    "Failed to remove ROP event callback on node '{}': {}".format(
                    self.renderNode.path(), str(e)))

        self.workItem = None
        self.hasCallback = False
        self.preFrame = None
        self.postFrame = None

class BakeTextureCooker(RopCooker):
    def __init__(self, work_item, server=None):
        RopCooker.__init__(self, work_item, server)

    def _cookSingleFrame(self):
        pre_cook_time = time.time()
        if not self._render():
            return (False, [])

        i = 1
        while True:
            outputparm = self.targetNode.parm('vm_uvoutputpicture' + str(i))
            if outputparm:
                exp = outputparm.evalAtFrame(self.frameRange[0])
                if exp:
                    outdir, basename = os.path.split(exp)
                    # the naming rules are crazy - just search for a prefix
                    firstvar = basename.find('%')
                    basename = basename[:firstvar].strip("_.")
                    globpattern = outdir + "/" + basename + "*"
                    files = glob.glob(globpattern)
                    for file_ in files:
                        if os.stat(file_).st_mtime > pre_cook_time:
                            self.workItem.addOutputFile(
                                file_, self.fileTags[0], own=True)
            else:
                break
            i += 1
        return (True, [])

    def _cookBatchFrames(self):
        raise RuntimeError("baketexture node not supported in batch mode")

class GamesBakerCooker(RopCooker):
    def __init__(self, work_item, server=None):
        RopCooker.__init__(self, work_item, server)

    def _cookSingleFrame(self):
        vm_tile_index = self.editNode.parm("vm_tile_index")
        if vm_tile_index is not None:
            vm_tile_index.set(self.tileIndex)

        pre_cook_time = time.time()

        # Since games baker is not a real rop, hq.render ignores the
        # frame range.  Since the frame range is not set to the desired range
        # in most cases we need to manually set the range parm here.
        frame_range = self.editNode.parmTuple("f")
        if frame_range:
            frame_range.deleteAllKeyframes()
            frame_range.set(self.frameRange)

        if not self._render():
            return (False, [])

        # sop and rop bakers have this parm
        if self.targetNode.parm('base_path'):
            exp = self.targetNode.parm('base_path').evalAtFrame(
                self.frameRange[0])
            if exp:
                outdir, basename = os.path.split(exp)
                globpattern = outdir + "/" + re.sub(r'\$\(\w+\)', '*', basename)
                for f in glob.glob(globpattern):
                    if os.stat(f).st_mtime > pre_cook_time:
                        self.workItem.addOutputFile(
                            f, self.fileTags[0], own=True)

        # rop_games_baker won't have this parm
        if self.targetNode.parm('export_fbx'):
            enable_fbx = self.targetNode.parm('export_fbx').evalAtFrame(
                self.frameRange[0])

            if enable_fbx:
                fbx_path = self.targetNode.parm('fbx_path').evalAtFrame(
                    self.frameRange[0])

                if fbx_path:
                    f = fbx_path
                    if os.path.exists(f) and os.stat(f).st_mtime > pre_cook_time:
                        self.workItem.addOutputFile(
                            f, self.fileTags[0], own=True)
        return (True, [])

    def _cookBatchFrames(self):
        raise RuntimeError("Gamesbaker nodes not supported in batch mode")

def cook(work_item):
    if not work_item.isInProcess:
        if not RopCooker.loadHIP(work_item):
            return False

    pdgjson.applyWedges(work_item)

    cooker = RopCooker.create(work_item)

    try:
        status, _ = cooker.cook(False)
    except:
        traceback.print_exc()
        status = False

    cooker.saveDebugFile()
    cooker.cleanup()
    return status

def main():
    main_parser = argparse.ArgumentParser()
    sub_parsers = main_parser.add_subparsers(dest='scriptcommand')

    # Work Item JSON parser
    json_parser = sub_parsers.add_parser(
        'json',
        description="Cooks a ROP node as a single frame job or as a batch")
    json_parser.add_argument(
        '--rpcstart', dest='rpcstart', action='store_true')

    args = main_parser.parse_args()

    if args.scriptcommand == "json":
        work_item = pdgjson.WorkItem.fromJobEnvironment(as_native=True)
        if args.rpcstart:
            work_item.setJobState(pdgjson.workItemState.Cooking)
        hou.exit(0 if cook(work_item) else 1)
    else:
        pdgjson.printHoudiniInfo()
        work_item.addWarning("Unsupport script command '{}'".format(
            args.scriptcommand))
        hou.exit(0)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        pdgcmd.printlog(traceback.format_exc())
        hou.exit(1)
