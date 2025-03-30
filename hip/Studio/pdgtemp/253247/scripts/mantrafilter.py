#
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
# NAME:	        mantrafilter.py ( Python )
#
# COMMENTS:     Output filter for rendered results produced by mantra, passed
#               to mantra with the -P argument.
#

try:
    from pdgcmd import addOutputFile
except ImportError:
    from pdgjob.pdgcmd import addOutputFile

def filterOutputAssets(assets):
    """
    mantra filter callback function
    """
    # asset file-type -> pdg data tag

    resulttag = { 0: 'image',
                  1: 'image/texture',
                  2: 'geo',
                  3: 'image/shadowmap',
                  4: 'image/photonmap',
                  5: 'image/envmap' }

    for asset in assets:
        for file_ in asset[1]:
            filename = file_[0]
            tag = ""
            try:
                tag = 'file/' + resulttag[file_[1]]
            except:
                pass
            addOutputFile(filename, result_data_tag=tag, raise_exc=False)
