#!/usr/bin/env python

""" runastrodriz.py - Module to control operation of astrodrizzle to
        remove distortion and combine HST images in the pipeline.

:License: :doc:`LICENSE`

USAGE: runastrodriz.py [-fhibng] inputFilename [newpath]

Alternative USAGE:
    python
    from acstools import runastrodriz
    runastrodriz.process(inputFilename,force=False,newpath=None,inmemory=False)

GUI Usage under Python:
    python
    from stsci.tools import teal
    import acstools
    cfg = teal.teal('runastrodriz')

PyRAF Usage:
    epar runastrodriz

If the '-i' option gets specified, no intermediate products will be written out
to disk. These products, instead, will be kept in memory. This includes all
single drizzle products (*single_sci and *single_wht), median image,
blot images, and crmask images.  The use of this option will therefore require
significantly more memory than usual to process the data.

If a value has been provided for the newpath parameter, all processing will be
performed in that directory/ramdisk.  The steps involved are:
   - create a temporary directory under that directory named after the input file
   - copy all files related to the input to that new directory
   - change to that new directory and run astrodrizzle
   - change back to original directory
   - move (not copy) ALL files from temp directory to original directory
   - delete temp sub-directory

The '-b' option will run this task in BASIC mode without creating headerlets
for each input image.

The '-n' option allows the user to specify the number of cores to be used in
running AstroDrizzle.

The '-g' option allows the user to TURN OFF alignment of the images to an external
astrometric catalog, such as GAIA, as accessible through the MAST interface.

Additional control over whether or not to attempt to align to an external
astrometric catalog, such as GAIA, is provided through the use of the
environment variables:

    - ASTROMETRY_COMPUTE_APOSTERIORI : Turn on/off alignment step.
      This environment variable will ALWAYS override any setting of the '-g' switch.
      Values (case-insensitive) can be 'on', 'off', 'yes', 'no'.

    - ASTROMETRY_APPLY_APRIORI : Replaces/resets ASTROMETRY_STEP_CONTROL
      variable used by `stwcs.updatewcs` to control whether or not a priori WCS
      solutions from the astrometry database should be applied to the data.
      If this is set, it will override any value set in the old variable.
      Values (case-insensitive) can be 'on','off','yes','no'.

*** INITIAL VERSION
W.J. Hack  12 Aug 2011: Initial version based on Version 1.2.0 of
                        STSDAS$pkg/hst_calib/wfc3/runwf3driz.py
W.J. Hack  27 Jun 2012: Implement support to process in different directory

W.J. Hack  24 Aug 2012: Provided interface for in-memory option

W.J. Hack  26 Nov 2012: Option to write out headerlets added and debugged

"""
# Import standard Python modules
import glob
import os
import shutil
import sys
import time
import logging

# THIRD-PARTY
from astropy.io import fits
from stsci.tools import fileutil, asnutil

__taskname__ = "runastrodriz"

# Local variables
__version__ = "1.6.0"
__version_date__ = "(01-Mar-2019)"

# Define parameters which need to be set specifically for
#    pipeline use of astrodrizzle
pipeline_pars = {'mdriztab': True,
                 'stepsize': 10,
                 'output': '',
                 'preserve': False,
                 'resetbits': 4096}

# default marker for trailer files
__trlmarker__ = '*** astrodrizzle Processing Version ' + __version__ + __version_date__ + '***\n'

envvar_bool_dict = {'off': False, 'on': True, 'no': False, 'yes': True, 'false': False, 'true': True}
envvar_dict = {'off': 'off', 'on': 'on', 'yes': 'on', 'no': 'off', 'true': 'on', 'false': 'off'}

envvar_compute_name = 'ASTROMETRY_COMPUTE_APOSTERIORI'
# Replace ASTROMETRY_STEP_CONTROL with this new related name
envvar_new_apriori_name = "ASTROMETRY_APPLY_APRIORI"
envvar_old_apriori_name = "ASTROMETRY_STEP_CONTROL"

# History:
# Version 1.0.0 - Derived from v1.2.0 of wfc3.runwf3driz to run astrodrizzle


# TEAL Interfaces
def getHelpAsString():
    helpString = 'runastrodriz Version ' + __version__ + __version_date__ + '\n'
    helpString += __doc__ + '\n'

    return helpString


def help():
    print(getHelpAsString())


def run(configobj=None):
    process(configobj['input'], force=configobj['force'],
                newpath=configobj['newpath'], inmemory=configobj['in_memory'],
                num_cores=configobj['num_cores'], headerlets=configobj['headerlets'])


# Primary user interface
def process(inFile, force=False, newpath=None, inmemory=False, num_cores=None,
            headerlets=True, align_to_gaia=True):
    """ Run astrodrizzle on input file/ASN table
        using default values for astrodrizzle parameters.
    """
    # We only need to import this package if a user run the task
    import drizzlepac
    from drizzlepac import processInput  # used for creating new ASNs for _flc inputs
    from stwcs import updatewcs
    from drizzlepac import alignimages

    # interpret envvar variable, if specified
    if envvar_compute_name in os.environ:
        val = os.environ[envvar_compute_name].lower()
        if val not in envvar_bool_dict:
            msg = "ERROR: invalid value for {}.".format(envvar_compute_name)
            msg += "  \n    Valid Values: on, off, yes, no, true, false"
            raise ValueError(msg)
        align_to_gaia = envvar_bool_dict[val]

    if envvar_new_apriori_name in os.environ:
        # Reset ASTROMETRY_STEP_CONTROL based on this variable
        # This provides backward-compatibility until ASTROMETRY_STEP_CONTROL
        # gets removed entirely.
        val = os.environ[envvar_new_apriori_name].lower()
        if val not in envvar_dict:
            msg = "ERROR: invalid value for {}.".format(envvar_new_apriori_name)
            msg += "  \n    Valid Values: on, off, yes, no, true, false"
            raise ValueError(msg)

        os.environ[envvar_old_apriori_name] = envvar_dict[val]

    if headerlets or align_to_gaia:
        from stwcs.wcsutil import headerlet

    # Open the input file
    try:
        # Make sure given filename is complete and exists...
        inFilename = fileutil.buildRootname(inFile, ext=['.fits'])
        if not os.path.exists(inFilename):
            print("ERROR: Input file - %s - does not exist." % inFilename)
            return
    except TypeError:
        print("ERROR: Inappropriate input file.")
        return

    # If newpath was specified, move all files to that directory for processing
    if newpath:
        orig_processing_dir = os.getcwd()
        new_processing_dir = _createWorkingDir(newpath, inFilename)
        _copyToNewWorkingDir(new_processing_dir, inFilename)
        os.chdir(new_processing_dir)

    # Initialize for later use...
    _mname = None
    _new_asn = None
    _calfiles = []

    # Identify WFPC2 inputs to account for differences in WFPC2 inputs
    wfpc2_input = fits.getval(inFilename, 'instrume') == 'WFPC2'
    cal_ext = None

    # Check input file to see if [DRIZ/DITH]CORR is set to PERFORM
    if '_asn' in inFilename:
        # We are working with an ASN table.
        # Use asnutil code to extract filename
        inFilename = _lowerAsn(inFilename)
        _new_asn = [inFilename]
        _asndict = asnutil.readASNTable(inFilename, None, prodonly=False)
        _cal_prodname = _asndict['output'].lower()
        # _fname = fileutil.buildRootname(_cal_prodname,ext=['_drz.fits'])

        # Retrieve the first member's rootname for possible use later
        _fimg = fits.open(inFilename, memmap=False)
        for name in _fimg[1].data.field('MEMNAME'):
            if name[-1] != '*':
                _mname = name.split('\0', 1)[0].lower()
                break
        _fimg.close()
        del _fimg

    else:
        # Check to see if input is a _RAW file
        # If it is, strip off the _raw.fits extension...
        _indx = inFilename.find('_raw')
        if _indx < 0: _indx = len(inFilename)
        # ... and build the CALXXX product rootname.
        if wfpc2_input:
            # force code to define _c0m file as calibrated product to be used
            cal_ext = ['_c0m.fits']
        _mname = fileutil.buildRootname(inFilename[:_indx], ext=cal_ext)

        _cal_prodname = inFilename[:_indx]
        # Reset inFilename to correspond to appropriate input for
        # drizzle: calibrated product name.
        inFilename = _mname

        if _mname is None:
            errorMsg = 'Could not find calibrated product!'
            raise Exception(errorMsg)

    # Create trailer filenames based on ASN output filename or
    # on input name for single exposures
    if '_raw' in inFile:
        # Output trailer file to RAW file's trailer
        _trlroot = inFile[:inFile.find('_raw')]
    elif '_asn' in inFile:
        # Output trailer file to ASN file's trailer, not product's trailer
        _trlroot = inFile[:inFile.find('_asn')]
    else:
        # Default: trim off last suffix of input filename
        # and replacing with .tra
        _indx = inFile.rfind('_')
        if _indx > 0:
            _trlroot = inFile[:_indx]
        else:
            _trlroot = inFile

    _trlfile = _trlroot + '.tra'

    # Open product and read keyword value
    # Check to see if product already exists...
    dkey = 'DRIZCORR'
    # ...if product does NOT exist, interrogate input file
    # to find out whether 'dcorr' has been set to PERFORM
    # Check if user wants to process again regardless of DRIZCORR keyword value
    if force:
        dcorr = 'PERFORM'
    else:
        if _mname:
            _fimg = fits.open(fileutil.buildRootname(_mname, ext=['_raw.fits']), memmap=False)
            _phdr = _fimg['PRIMARY'].header
            if dkey in _phdr:
                dcorr = _phdr[dkey]
            else:
                dcorr = None
            _fimg.close()
            del _fimg
        else:
            dcorr = None

    time_str = _getTime()
    _tmptrl = _trlroot + '_tmp.tra'
    _drizfile = _trlroot + '_pydriz'
    _drizlog = _drizfile + ".log"  # the '.log' gets added automatically by astrodrizzle
    _alignlog = _trlroot + '_align.log'
    _alignlog_copy = _alignlog.replace('.log', '_copy.log')
    if dcorr == 'PERFORM':
        if '_asn.fits' not in inFilename:
            # Working with a singleton
            # However, we always want to make sure we always use
            # a calibrated product as input, if available.
            _infile = fileutil.buildRootname(_cal_prodname, ext=cal_ext)
            _infile_flc = fileutil.buildRootname(_cal_prodname, ext=['_flc.fits'])

            _cal_prodname = _infile
            _inlist = _calfiles = [_infile]

            # Add CTE corrected filename as additional input if present
            if os.path.exists(_infile_flc) and _infile_flc != _infile:
                _calfiles_flc = [_infile_flc]

        else:
            # Working with an ASN table...
            _infile = inFilename
            flist, duplist = processInput.checkForDuplicateInputs(_asndict['order'])
            _calfiles = flist
            if len(duplist) > 0:
                origasn = processInput.changeSuffixinASN(inFilename, 'flt')
                dupasn = processInput.changeSuffixinASN(inFilename, 'flc')
                _inlist = [origasn, dupasn]
            else:
                _inlist = [_infile]
            # We want to keep the original specification of the calibration
            # product name, though, not a lower-case version...
            _cal_prodname = inFilename
            _new_asn.extend(_inlist)  # kept so we can delete it when finished

            # check to see whether FLC files are also present, and need to be updated
            # generate list of FLC files
            _calfiles_flc = [f.replace('_flt.fits', '_flc.fits')
                             for f in _calfiles
                             if os.path.exists(f.replace('_flt.fits', '_flc.fits'))]

        align_files = None

        # insure these files exist, if not, blank them out
        # Also pick out what files will be used for additional alignment to GAIA
        if not os.path.exists(_calfiles_flc[0]):
            _calfiles_flc = None
            align_files = _calfiles
            align_update_files = None
        else:
            align_files = _calfiles_flc
            align_update_files = _calfiles

        # Run updatewcs on each list of images
        updatewcs.updatewcs(_calfiles)
        if _calfiles_flc:
            updatewcs.updatewcs(_calfiles_flc)

        if align_to_gaia:
            # Perform additional alignment on the FLC files, if present
            ###############
            #
            # call hlapipeline code here on align_files list of files
            #
            ###############
            # Create trailer marker message for start of align_to_GAIA processing
            _trlmsg = _timestamp("Align_to_GAIA started ")
            print(_trlmsg)
            ftmp = open(_tmptrl, 'w')
            ftmp.writelines(_trlmsg)
            ftmp.close()
            _appendTrlFile(_trlfile, _tmptrl)
            _trlmsg = ""

            # Create an empty astropy table so it can be used as input/output for the perform_align function
            try:
                align_table = alignimages.perform_align(align_files, update_hdr_wcs=True, runfile=_alignlog,
                                                        clobber=False)
                for row in align_table:
                    if row['status'] == 0:
                        trlstr = "Successfully aligned {} to {} astrometric frame\n"
                        _trlmsg += trlstr.format(row['imageName'], row['catalog'])
                    else:
                        trlstr = "Could not align {} to absolute astrometric frame\n"
                        _trlmsg += trlstr.format(row['imageName'])

            except Exception:
                # Something went wrong with alignment to GAIA, so report this in
                # trailer file
                _trlmsg = "EXCEPTION encountered in alignimages...\n"
                _trlmsg += "   No correction to absolute astrometric frame applied!\n"

            # Write the perform_align log to the trailer file...(this will delete the _alignlog)
            shutil.copy(_alignlog, _alignlog_copy)
            _appendTrlFile(_trlfile, _alignlog_copy)

            # Append messages from this calling routine post-perform_align
            ftmp = open(_tmptrl, 'w')
            ftmp.writelines(_trlmsg)
            ftmp.close()
            _appendTrlFile(_trlfile, _tmptrl)
            _trlmsg = ""

            # Check to see whether there are any additional input files that need to
            # be aligned (namely, FLT images)
            if align_update_files and align_table:
                # Apply headerlets from alignment to FLT version of the files
                for fltfile, flcfile in zip(align_update_files, align_files):
                    row = align_table[align_table['imageName'] == flcfile]
                    headerletFile = row['headerletFile'][0]
                    if headerletFile != "None":
                        headerlet.apply_headerlet_as_primary(fltfile, headerletFile,
                                                            attach=True, archive=True)
                        # append log file contents to _trlmsg for inclusion in trailer file
                        _trlstr = "Applying headerlet {} as Primary WCS to {}\n"
                        _trlmsg += _trlstr.format(headerletFile, fltfile)
                    else:
                        _trlmsg += "No absolute astrometric headerlet applied to {}\n".format(fltfile)

            # Finally, append any further messages associated with alignement from this calling routine
            _trlmsg += _timestamp('Align_to_GAIA completed ')
            print(_trlmsg)
            ftmp = open(_tmptrl, 'w')
            ftmp.writelines(_trlmsg)
            ftmp.close()
            _appendTrlFile(_trlfile, _tmptrl)

        # Run astrodrizzle and send its processing statements to _trlfile
        _pyver = drizzlepac.astrodrizzle.__version__

        for _infile in _inlist:  # Run astrodrizzle for all inputs
            # Create trailer marker message for start of astrodrizzle processing
            _trlmsg = _timestamp('astrodrizzle started ')
            _trlmsg += __trlmarker__
            _trlmsg += '%s: Processing %s with astrodrizzle Version %s\n' % (time_str, _infile, _pyver)
            print(_trlmsg)

            # Write out trailer comments to trailer file...
            ftmp = open(_tmptrl, 'w')
            ftmp.writelines(_trlmsg)
            ftmp.close()
            _appendTrlFile(_trlfile, _tmptrl)

            _pyd_err = _trlroot + '_pydriz.stderr'

            try:
                drizzlepac.astrodrizzle.AstroDrizzle(input=_infile, runfile=_drizfile,
                                            configobj='defaults', in_memory=inmemory,
                                            num_cores=num_cores, **pipeline_pars)
            except Exception as errorobj:
                _appendTrlFile(_trlfile, _drizlog)
                _appendTrlFile(_trlfile, _pyd_err)
                _ftrl = open(_trlfile, 'a')
                _ftrl.write('ERROR: Could not complete astrodrizzle processing of %s.\n' % _infile)
                _ftrl.write(str(sys.exc_info()[0]) + ': ')
                _ftrl.writelines(str(errorobj))
                _ftrl.write('\n')
                _ftrl.close()
                print('ERROR: Could not complete astrodrizzle processing of %s.' % _infile)
                raise Exception(str(errorobj))

            # Now, append comments created by PyDrizzle to CALXXX trailer file
            print('Updating trailer file %s with astrodrizzle comments.' % _trlfile)
            _drizlog_copy = _drizlog.replace('.log', '_copy.log')
            shutil.copy(_drizlog, _drizlog_copy)
            _appendTrlFile(_trlfile, _drizlog_copy)

        # Save this for when astropy.io.fits can modify a file 'in-place'
        # Update calibration switch
        _fimg = fits.open(_cal_prodname, mode='update', memmap=False)
        _fimg['PRIMARY'].header[dkey] = 'COMPLETE'
        _fimg.close()
        del _fimg

        # Enforce pipeline convention of all lower-case product
        # names
        _prodlist = glob.glob('*drz.fits')
        for _prodname in _prodlist:
            _plower = _prodname.lower()
            if _prodname != _plower: os.rename(_prodname, _plower)

    else:
        # Create default trailer file messages when astrodrizzle is not
        # run on a file.  This will typically apply only to BIAS,DARK
        # and other reference images.
        # Start by building up the message...
        _trlmsg = _timestamp('astrodrizzle skipped ')
        _trlmsg = _trlmsg + __trlmarker__
        _trlmsg = _trlmsg + '%s: astrodrizzle processing not requested for %s.\n' % (time_str, inFilename)
        _trlmsg = _trlmsg + '       astrodrizzle will not be run at this time.\n'
        print(_trlmsg)

        # Write message out to temp file and append it to full trailer file
        ftmp = open(_tmptrl, 'w')
        ftmp.writelines(_trlmsg)
        ftmp.close()
        _appendTrlFile(_trlfile, _tmptrl)

    # Append final timestamp to trailer file...
    _final_msg = '%s: Finished processing %s \n' % (time_str, inFilename)
    _final_msg += _timestamp('astrodrizzle completed ')
    _trlmsg += _final_msg
    ftmp = open(_tmptrl, 'w')
    ftmp.writelines(_trlmsg)
    ftmp.close()
    _appendTrlFile(_trlfile, _tmptrl)

    # If we created a new ASN table, we need to remove it
    if _new_asn is not None:
        for _name in _new_asn: fileutil.removeFile(_name)

    # Clean up any generated OrIg_files directory
    if os.path.exists("OrIg_files"):
        # check to see whether this directory is empty
        flist = glob.glob('OrIg_files/*.fits')
        if len(flist) == 0:
            os.rmdir("OrIg_files")
        else:
            print('OrIg_files directory NOT removed as it still contained images...')

    # If headerlets have already been written out by alignment code,
    # do NOT write out this version of the headerlets
    if headerlets:
        # Generate headerlets for each updated FLT image
        hlet_msg = _timestamp("Writing Headerlets started")
        for fname in _calfiles:
            frootname = fileutil.buildNewRootname(fname)
            hname = "%s_flt_hlet.fits" % frootname
            # Write out headerlet file used by astrodrizzle, however,
            # do not overwrite any that was already written out by alignimages
            if not os.path.exists(hname):
                hlet_msg += "Created Headerlet file %s \n" % hname
                try:
                    headerlet.write_headerlet(fname, 'OPUS', output='flt', wcskey='PRIMARY',
                        author="OPUS", descrip="Default WCS from Pipeline Calibration",
                        attach=False, clobber=True, logging=False)
                except ValueError:
                    hlet_msg += _timestamp("SKIPPED: Headerlet not created for %s \n" % fname)
                    # update trailer file to log creation of headerlet files
        hlet_msg += _timestamp("Writing Headerlets completed")
        ftrl = open(_trlfile, 'a')
        ftrl.write(hlet_msg)
        ftrl.close()

    # Remove secondary log files for good...
    logging.shutdown()
    for _olog in [_alignlog, _drizlog]:
        if os.path.exists(_olog):
            os.remove(_olog)

    # If processing was done in a temp working dir, restore results to original
    # processing directory, return to original working dir and remove temp dir
    if newpath:
        _restoreResults(new_processing_dir, orig_processing_dir)
        os.chdir(orig_processing_dir)
        _removeWorkingDir(new_processing_dir)

    # Provide feedback to user
    print(_final_msg)

def _lowerAsn(asnfile):
    """ Create a copy of the original asn file and change
        the case of all members to lower-case.
    """
    # Start by creating a new name for the ASN table
    _indx = asnfile.find('_asn.fits')
    _new_asn = asnfile[:_indx] + '_pipeline' + asnfile[_indx:]
    if os.path.exists(_new_asn):
        os.remove(_new_asn)
    # copy original ASN table to new table
    shutil.copy(asnfile, _new_asn)

    # Open up the new copy and convert all MEMNAME's to lower-case
    fasn = fits.open(_new_asn, mode='update', memmap=False)
    for i in range(len(fasn[1].data)):
        fasn[1].data[i].setfield('MEMNAME', fasn[1].data[i].field('MEMNAME').lower())
    fasn.close()

    return _new_asn

def _appendTrlFile(trlfile, drizfile):
    """ Append drizfile to already existing trlfile from CALXXX.
    """
    if not os.path.exists(drizfile):
        return
    # Open already existing CALWF3 trailer file for appending
    ftrl = open(trlfile, 'a')
    # Open astrodrizzle trailer file
    fdriz = open(drizfile)

    # Read in drizzle comments
    _dlines = fdriz.readlines()

    # Append them to CALWF3 trailer file
    ftrl.writelines(_dlines)

    # Close all files
    ftrl.close()
    fdriz.close()

    # Now, clean up astrodrizzle trailer file
    os.remove(drizfile)


def _timestamp(_process_name):
    """Create formatted time string recognizable by OPUS."""
    _prefix = time.strftime("%Y%j%H%M%S-I-----", time.localtime())
    _lenstr = 60 - len(_process_name)
    return _prefix + _process_name + (_lenstr * '-') + '\n'

def _getTime():
    # Format time values for keywords IRAF-TLM, and DATE
    _ltime = time.localtime(time.time())
    time_str = time.strftime('%H:%M:%S (%d-%b-%Y)', _ltime)

    return time_str

# Functions used to manage processing in a separate directory/ramdisk
def _createWorkingDir(rootdir, input):
    """
    Create a working directory based on input name under the parent directory specified as rootdir
    """
    # extract rootname from input
    rootname = input[:input.find('_')]
    newdir = os.path.join(rootdir, rootname)
    if not os.path.exists(newdir):
        os.mkdir(newdir)
    return newdir

def _copyToNewWorkingDir(newdir, input):
    """ Copy input file and all related files necessary for processing to the new working directory.

        This function works in a greedy manner, in that all files associated
        with all inputs(have the same rootname) will be copied to the new
        working directory.
    """
    flist = []
    if '_asn.fits' in input:
        asndict = asnutil.readASNTable(input, None)
        flist.append(input[:input.find('_')])
        flist.extend(asndict['order'])
        flist.append(asndict['output'])
    else:
        flist.append(input[:input.find('_')])
    # copy all files related to these rootnames into new dir
    for rootname in flist:
        for fname in glob.glob(rootname + '*'):
            shutil.copy(fname, os.path.join(newdir, fname))

def _restoreResults(newdir, origdir):
    """ Move (not copy) all files from newdir back to the original directory
    """
    for fname in glob.glob(os.path.join(newdir, '*')):
        shutil.move(fname, os.path.join(origdir, os.path.basename(fname)))

def _removeWorkingDir(newdir):
    """ Delete working directory
    """
    os.rmdir(newdir)


# Functions to support execution from the shell.
def main():

    import getopt

    try:
        optlist, args = getopt.getopt(sys.argv[1:], 'bhfgin:')
    except getopt.error as e:
        print(str(e))
        print(__doc__)
        print("\t", __version__)

    # initialize default values
    help = 0
    force = False
    newdir = None
    inmemory = False
    num_cores = None
    headerlets = True
    align_to_gaia = True

    # read options
    for opt, value in optlist:
        if opt == "-g":
            align_to_gaia = False
        if opt == "-h":
            help = 1
        if opt == "-f":
            force = True
        if opt == "-i":
            inmemory = True
        if opt == '-n':
            if not value.isdigit():
                print('ERROR: num_cores value must be an integer!')
                raise ValueError
            num_cores = int(value)
        if opt == '-b':
            # turn off writing headerlets
            headerlets = False
    if len(args) < 1:
        print("syntax: runastrodriz.py [-fhibng] inputFilename [newpath]")
        sys.exit()
    if len(args) > 1:
        newdir = args[-1]
    if (help):
        print(__doc__)
        print("\t", __version__ + '(' + __version_date__ + ')')
    else:
        try:
            process(args[0], force=force, newpath=newdir, inmemory=inmemory,
                    num_cores=num_cores, headerlets=headerlets,
                    align_to_gaia=align_to_gaia)
        except Exception as errorobj:
            print(str(errorobj))
            print("ERROR: Cannot run astrodrizzle on %s." % sys.argv[1])
            raise Exception(str(errorobj))

    sys.exit()


if __name__ == "__main__":
    main()
