#!/usr/bin/python
# -*- coding: UTF8 -*-

"""
Module containing code to verify the format of a SAFE project Excel dataset.

The functions are written to extract as much information as possible from
checking a file so, rather than raising an error and halting at the first
fault, functions are written to check as much as they can on the inputs.

Metadata is stored in a Dataset object, which also keeps a record of any
information and warnings issued during processing. These are optionally
printed to screen during processing but can also be retrieved from the
Dataset object.

Taxon names and locations are both validated:
i)  By default, taxonomic names are validated against the GBIF backbone taxonomy
    using API queries over the internet. A user can download and build a local
    sqlite copy of the database to avoid the need for a web connection and to
    improve the speed of validation.

ii) Locations are validated against a list of valid locations. By default, this
    is downloaded from a web service provided from the SAFE website, but a local
    file can be provided for off line use.
"""

from __future__ import print_function
import os
import datetime
from dateutil import parser
import argparse
import re
from collections import Counter
from StringIO import StringIO
from shapely import wkt
from shapely.errors import WKTReadingError
import numbers
import logging
import sqlite3
import xlrd
import requests
import simplejson

# define some regular expressions used to check validity
RE_ORCID = re.compile(r'[0-9]{4}-[0-9]{4}-[0-9]{4}-[0-9]{3}[0-9X]')
RE_EMAIL = re.compile(r'\S+@\S+\.\S+')
RE_NAME = re.compile(r'[^,]+,[ ]?[^,]+')
RE_WSPACE_ONLY = re.compile(r'^\s*$')
RE_CONTAINS_WSPACE = re.compile(r'\s')
RE_CONTAINS_PUNC = re.compile(r'[,;:]')
RE_DOI = DOI = re.compile('https?://(dx.)?doi.org/')

# note that logically the next one also catches whitespace at both ends
RE_WSPACE_AT_ENDS = re.compile(r'^\s+.+|.+\s+$')
RE_DMS = re.compile(r'[°\'"dms’”]+')

# Taxon levels used in GBIF taxonomy. We explicitly exclude form and variety
# because they cannot be matched into the backbone hierarchy without extra
# API calls
BACKBONE_TYPES = ['kingdom', 'phylum', 'order', 'class', 'family',
                  'genus', 'species', 'subspecies']


# Logger setup - setup the standard logger to provide
# i)   A handler providing a counter of the number of calls to each log level
# ii)  A formatter to provide user controlled indentation and to
#      code levels as symbols for compactness
# iii) A handler writing to a global log file written to a StringIO container
# iv)  Optionally a handler also writing the log to stdout can be added when
#      a Dataset instance is created.

# Note that the handlers are created when the module is loaded, so when running
# behind a web server, the content of the handlers persist between runs of the code.
# To avoid constant concatenation of outputs, Dataset.__init__() empties them.


class CounterHandler(logging.Handler):
    """
    Handler instance that maintains a count of calls at each log level
    """

    def __init__(self, *args, **kwargs):
        logging.Handler.__init__(self, *args, **kwargs)
        self.counters = {'DEBUG': 0, 'INFO': 0, 'WARNING': 0, 'ERROR': 0, 'CRITICAL': 0}

    def emit(self, rec):
        self.counters[rec.levelname] += 1

    def reset(self):
        self.counters = {'DEBUG': 0, 'INFO': 0, 'WARNING': 0, 'ERROR': 0, 'CRITICAL': 0}


class IndentFormatter(logging.Formatter):
    """
    A formatter that provides an indentation depth and encodes the logging
    levels as single character strings. The extra argument to logger messages
    can be used to provide a dictionary to set:
        - 'join': a list of entries to join as comma separated list on
          to the end of the message.
        - 'quote': a flag to set joined entries to be quoted to show
          whitespace around values.
        - 'indent_before' or 'indent_after': two options to set the indent
          depth of the formatter.
    """

    def __init__(self, fmt=None, datefmt=None):
        logging.Formatter.__init__(self, fmt, datefmt)
        self.depth = 0

    def format(self, rec):

        # adjust indent before
        if hasattr(rec, 'indent_before'):
            self.depth = int(rec.indent_before)

        # store so users don't have to provide both
        rec.indent_before = self.depth

        rec.indent = '    ' * self.depth
        # encode level
        codes = {'DEBUG': '>', 'INFO': '-', 'WARNING': '?', 'ERROR': '!', 'CRITICAL': '*'}
        rec.levelcode = codes[rec.levelname]
        # format message
        msg = logging.Formatter.format(self, rec)
        # add any joined values
        if hasattr(rec, 'join'):
            if hasattr(rec, 'quote') and rec.quote:
                # quote if requested and then avoid requoting if the
                # formatter is emitting to more than one stream handler
                rec.join = ["'" + unicode(o) + "'" for o in rec.join]
                del rec.quote
            msg += ', '.join(rec.join)

        # adjust indent after
        if hasattr(rec, 'indent_after'):
            self.depth = int(rec.indent_after)

        # store so users don't have to provide both
        rec.indent_after = self.depth

        return msg


# Setup the logging instance
LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.DEBUG)

# Add the counter handler
CH = CounterHandler()
CH.setLevel(logging.DEBUG)
LOGGER.addHandler(CH)

# Create the formatter
FORMATTER = IndentFormatter("%(indent)s%(levelcode)s %(message)s")

# Create a StringIO object to hold the log, set a stream handler to use that,
# attach the custom formatter and add it to the logger. LOG will then contain
# a complete record of logging messages.
LOG = StringIO()
LOG_SH = logging.StreamHandler(LOG)
LOG_SH.setFormatter(FORMATTER)
LOGGER.addHandler(LOG_SH)

"""
Some static functions
"""


def is_blank(value):
    """
    Helper function that checks if a value is None or contains only whitespace
    Args:
        value: A single value

    Returns:
        Boolean
    """

    return (value is None) or (RE_WSPACE_ONLY.match(unicode(value)) is not None)


def is_padded(value):
    """
    Helper function that checks if the value is padded with whitespace
    Args:
        value: Contents of a cell

    Returns:
        Boolean
    """

    return (value is not None) and bool(RE_WSPACE_AT_ENDS.match(unicode(value)))


def duplication(data):
    """
    Looks for duplication in a list of strings, returning the duplicated elements.

    Args:
        data: A list of strings

    Returns:
        A list of duplicated elements
    """

    return [ky for ky, vl in Counter(data).iteritems() if vl > 1]


def to_lowercase(str_vals):
    """
    Converts a list of string values to lowercase, handling empty strings and None
    via the is_blank function.

    Args:
        str_vals: A list of string values

    Returns:
        A list of lower-case string values, including None for blank entries.
    """

    return [v.lower() if not is_blank(v) else None for v in str_vals]


def is_numeric_string(txt):
    """
    Checks if a string value can represent an float.
    Args:
        txt: A string

    Returns:
        A boolean.
    """
    try:
        float(txt)
        return True
    except ValueError:
        return False


def all_numeric(data):
    """
    Tests if all values in an iterable are numeric.
    Args:
        data: An iterable claiming to contain numbers

    Returns:
        A list containing a boolean indicating whether all of the
        values were numbers and then a list of the genuine numeric values
    """

    nums = [dt for dt in data if isinstance(dt, numbers.Number)]

    return [len(nums) == len(data), nums]


def web_gbif_validate(tax, rnk, gbif_id=None):
    """
    Validates a taxon name and rank against the GBIF web API. It uses the API endpoint
    species/match?name=XXX&rank=YYY&strict=true
    endpoint to



    # safe to assume that the next least nested taxonomic level is the parent.
    # So, populate GBIF ID using species/match and then use species/{id} to populate the
    # return values

    Args:
        tax (str, None): The (case insensitive) taxon name
        rnk (str, None): The taxon rank
        gbif_id (int): Optional GBIF taxon id to resolve ambiguous taxon/rank
            combinations. If gbif_id is not None, then tax and rank can be
            None and the GBIF response will be used to populate those values.

    Returns:
        A dictionary with the following possible keys:
            'status': the outcome of the lookup with one of the following values:
                found, no_match, validation_fail, unknown_id, id_mismatch
            'user': an index tuple of the user provided information for synonymous usages
            'canon': an index tuple of the canonical information
            'hier': a list of 2-tuples of rank and GBIF ID for the taxonomic hierarchy
            'note': a string of any extra information provided by the search

        An index tuple has the structure: (taxon ID, parent ID, name, rank, taxonomic status)
    """

    if gbif_id is None:
        # If no ID is provided then use the species/match?name={} endpoint to
        # try and find an ID for the combination
        url = u"http://api.gbif.org/v1/species/match?name={}&rank={}&strict=true".format(tax, rnk)
        tax_gbif = requests.get(url)

        # failures here suggest some kind of gateway problem
        if tax_gbif.status_code != 200:
            return {'status': 'validation_fail'}

        resp = tax_gbif.json()

        # check the response status
        if resp['matchType'] == u'NONE':
            # No match found - look for explanatory notes
            if 'note' in resp:
                return {'status': 'no_match', 'note': resp['note']}
            else:
                return {'status': 'no_match'}
        else:
            gbif_id = resp['usageKey']

    # Now use the species/{id} endpoint
    url = u"http://api.gbif.org/v1/species/{}".format(gbif_id)
    tax_gbif = requests.get(url)

    # unknown ID numbers return a 404 error
    if tax_gbif.status_code == 404:
        return {'status': 'unknown_id'}
    elif tax_gbif.status_code != 200:
        return {'status': 'validation_fail'}

    resp = tax_gbif.json()

    # If they were provided, check tax and rnk are compatible with the ID number
    if ((tax is not None and tax != resp['canonicalName']) or
            (rnk is not None and rnk.lower() != resp['rank'].lower())):
        return {'status': 'id_mismatch'}

    # Now we should have a single usage in resp so populate the return dictionaries and
    # set the response to be used for the hierarchy.

    # First, fill in the parent key if it is missing, which it is for Kingdom level taxa
    if resp['rank'] == 'KINGDOM':
        resp['parentKey'] = None

    if resp['taxonomicStatus'].lower() in ('accepted', 'doubtful'):
        # populate the return values
        ret = {'status': 'found',
               'canon': (resp['key'], resp['parentKey'], resp['canonicalName'],
                         resp['rank'].lower(), resp['taxonomicStatus'].lower())}
        hier_resp = resp
    else:
        # look up the accepted usage
        usage_url = u"http://api.gbif.org/v1/species/{}".format(resp['acceptedKey'])
        accept = requests.get(usage_url)

        if accept.status_code != 200:
            return {'status': 'validation_fail'}
        else:
            acpt = accept.json()

        # populate the return values
        ret = {'status': 'found',
               'user': (resp['key'], resp['parentKey'], resp['canonicalName'],
                        resp['rank'].lower(), resp['taxonomicStatus'].lower()),
               'canon': (acpt['key'], acpt['parentKey'], acpt['canonicalName'],
                         acpt['rank'].lower(), acpt['taxonomicStatus'].lower())}

        hier_resp = acpt

    # Add the taxonomic hierarchy from the accepted usage
    ret['hier'] = [(rk, hier_resp[ky])
                   for rk, ky in [('kingdom', 'kingdomKey'), ('phylum', 'phylumKey'),
                                  ('class', 'classKey'), ('order', 'orderKey'),
                                  ('family', 'familyKey'), ('genus', 'genusKey'),
                                  ('species', 'speciesKey')]
                   if ky in hier_resp]

    # return the details
    return ret


def local_gbif_validate(conn, tax, rnk, gbif_id=None):
    """
    Validates a taxon name and rank against a connection to a local GBIF database.

    Args:
        conn (sqlite3.Connection): An sqlite3 connection to the backbone database
        tax (str, None): The (case sensitive) taxon name
        rnk (str, None): The taxon rank
        gbif_id (int): Optional GBIF taxon id to resolve ambiguous taxon/rank
            combinations. If gbif_id is not None, then tax and rank can be
            None and the GBIF response will be used to populate those values.

    Returns:
        A dictionary with the following possible keys:
            'status': the outcome of the lookup with one of the following values:
                found, no_match, validation_fail, unknown_id, id_mismatch
            'user': an index tuple of the user provided information for synonymous usages
            'canon': an index tuple of the canonical information
            'hier': a list of 2-tuples of rank and GBIF ID for the taxonomic hierarchy
            'note': a string of any extra information provided by the search

        An index tuple has the structure: (taxon ID, parent ID, name, rank, taxonomic status)
    """

    # Make sure the connection is returning results as sqlite.Row objects
    if conn.row_factory != sqlite3.Row:
        conn.row_factory = sqlite3.Row

    # This if else section either runs through with a single Row assigned
    # to tax_gbif or exits returning an error condition of some sort
    if gbif_id is not None:
        # get the record associated with the provided ID
        tax_sql = ("select * from backbone where id = {}".format(gbif_id))
        tax_gbif = conn.execute(tax_sql).fetchone()

        # check there is a result and that it is congruent with any
        # provided taxon or rank information
        if tax_gbif is None:
            return {'status': 'unknown_id'}
        elif ((tax is not None and tax_gbif['canonical_name'] != tax) or
              (rnk is not None and tax_gbif['rank'].lower() != rnk.lower())):
            return {'status': 'id_mismatch'}

    else:
        # get the set of records associated with the taxon or rank
        tax_sql = ("select * from backbone where canonical_name ='{}' and "
                   "rank= '{}';".format(tax, rnk.upper()))
        tax_gbif = conn.execute(tax_sql).fetchall()

        if len(tax_gbif) == 0:
            # No matching rows
            return {'status': 'no_match'}
        elif len(tax_gbif) == 1:
            # one matching row - extract it from the list
            tax_gbif = tax_gbif[0]
        else:
            # More than one row - try to mimic the preferred hits reported
            # by the GBIF API to select a single hit by looking at the counts
            # of the different statuses.

            # First, get the taxon statuses
            tx_status = [tx['status'].lower() for tx in tax_gbif]
            tx_counts = Counter(tx_status)

            if 'accepted' in tx_counts.keys():
                # Accepted hits are first preference
                if tx_counts['accepted'] == 1:
                    # only one accepted match alongside other usages so extract that hit
                    tax_gbif = tax_gbif[tx_status.index('accepted')]
                else:
                    # more than one accepted match, so return no match and a note, as
                    # the API interface does.
                    return {'status': 'no_match',
                            'note': 'Multiple equal matches for {}'.format(tax)}

            elif 'doubtful' in tx_counts.keys():
                # Doubtful hits get next preference - not quite sure about this!
                if tx_counts['doubtful'] == 1:
                    # only one doubtful match alongside other usages so extract that hit
                    tax_gbif = tax_gbif[tx_status.index('doubtful')]
                else:
                    # more than one doubtful match, so return no match and a note, as
                    # the API interface does.
                    return {'status': 'no_match',
                            'note': 'Multiple equal matches for {}'.format(tax)}

            else:
                # Rows now contain only synonyms (of varying kinds) and misapplied. Both of
                # these types have accepted usage values, so look for a unique accepted usage,
                # trapping the edge case of kingdoms, which have no parent_key.

                tx_acc = {tx['parent_key'] for tx in tax_gbif if tx['parent_key'] is not None}

                if len(tx_acc) > 1:
                    # More than one accepted usage
                    return {'status': 'no_match',
                            'note': 'Multiple equal matches for {}'.format(tax)}
                else:
                    # A single accepted usage - pick the first row to index
                    tax_gbif = tax_gbif[0]

    # Should now have a single row for the preferred hit, so package that up and set
    # what is going to be used to build the hierarchy
    if tax_gbif['status'].lower() in ['accepted', 'doubtful']:
        ret = {'status': 'found',
               'canon': (tax_gbif['id'], tax_gbif['parent_key'], tax_gbif['canonical_name'],
                         tax_gbif['rank'].lower(), tax_gbif['status'].lower())}
        hier_row = tax_gbif
    else:
        # Look up the parent_key, which is the accepted usage key for synonyms.
        acc_sql = 'select * from backbone where id = {};'.format(tax_gbif['parent_key'])
        acc_gbif = conn.execute(acc_sql).fetchone()
        # fill in the return. The use of the accepted taxon parent key for the user entry
        # is deliberate: it points up the hierarchy not to the accepted taxon.
        ret = {'status': 'found',
               'canon': (acc_gbif['id'], acc_gbif['parent_key'], acc_gbif['canonical_name'],
                         acc_gbif['rank'].lower(), acc_gbif['status'].lower()),
               'user': (tax_gbif['id'], acc_gbif['parent_key'], tax_gbif['canonical_name'],
                        tax_gbif['rank'].lower(), tax_gbif['status'].lower())}
        hier_row = acc_gbif

    # Add the taxonomic hierarchy from the preferred usage
    ret['hier'] = [(rk, hier_row[ky])
                   for rk, ky in [('kingdom', 'kingdom_key'), ('phylum', 'phylum_key'),
                                  ('class', 'class_key'), ('order', 'order_key'),
                                  ('family', 'family_key'), ('genus', 'genus_key'),
                                  ('species', 'species_key')]
                   if hier_row[ky] is not None]

    return ret


"""
Two classes to handle datasets and the data worksheets they contain
"""


class DataWorksheet(object):
    """
    This is just a container for the metadata on a data worksheet.
    It basically only exists to set defaults on a data worksheet and
    to provide dot notation rather than using dictionary labels.

    Args:
        meta: The dictionary of worksheet name, title and description
        loaded from the summary worksheet.
    """

    def __init__(self, meta):
        # set defaults
        self.name = meta['name']
        self.description = meta['description']
        self.title = meta['title']
        self.max_row = 0
        self.max_col = 0
        self.descriptors = None
        self.taxa_fields = None
        self.field_name_row = None
        self.fields = []
        self.external = None


class Dataset(object):
    """
    This class provides methods to load and store the metadata associated
    with a dataset stored in a SAFE project formatted Excel file.

    Args:
        filename: The Excel file from which to read the Dataset
        verbose: Should the reporting print to the screen during runtime
        gbif_database: A local path to an sqlite database containing the GBIF database
        to be used instead of the web API.
    """

    def __init__(self, filename, verbose=True, gbif_database=None):

        try:
            self.workbook = xlrd.open_workbook(filename=filename)
        except IOError:
            raise IOError('Could not open file {}'.format(filename))

        self.filename = os.path.basename(filename)

        # report tracking - clear out previous content in the logger. When the function runs
        # on a webserver, the same logger instance is used for all runs, so the log contents
        # persist unless they are tidied up.
        CH.reset()
        LOG.truncate(0)

        if verbose:
            # Look to see if the logger already has a verbose handler writing to the console and
            # add one if it doesn't. This is primarly to provide output for command line usage.
            handler_names = [handler.get_name() for handler in LOGGER.handlers]
            if 'console_log' not in handler_names:
                console_log = logging.StreamHandler()
                console_log.setFormatter(FORMATTER)
                console_log.set_name('console_log')
                LOGGER.addHandler(console_log)
        elif not verbose:
            # Remove the console_log handler if one exists
            handler_names = [handler.get_name() for handler in LOGGER.handlers]
            if 'console_log' in handler_names:
                console_log_handler = LOGGER.handlers[handler_names.index('console_log')]
                LOGGER.removeHandler(console_log_handler)

        LOGGER.info("Checking file '{}'".format(filename),
                    extra={'indent_before': 0, 'indent_after': 1})

        # basic info
        self.sheet_names = set(self.workbook.sheet_names())

        # initialise data contents
        self.project_id = None
        self.authors = []
        self.title = None
        self.description = None
        self.dataworksheet_summaries = []
        self.access = 'Open'
        self.embargo_date = None
        self.publication_doi = []
        self.dataworksheets = []
        self.keywords = []
        self.temporal_extent = None
        self.latitudinal_extent = None
        self.longitudinal_extent = None
        self.locations = set()
        self.location_index = set()
        self.locations_used = set()
        self.taxon_names = set()
        self.taxon_names_used = set()
        self.taxon_index = set()
        self.external_files = []
        self.funders = []
        self.passed = False

        # Setup the taxonomy validation mechanism.
        if gbif_database is None:
            LOGGER.info('Using GBIF online API to validate taxonomy')
            self.use_local_gbif = False
            self.gbif_conn = None
        else:
            # If a GBIF file is provided, does the file exist?
            if not os.path.exists(gbif_database):
                LOGGER.info('Local GBIF database not found, defaulting to online API')
                self.use_local_gbif = False
                self.gbif_conn = None

            # And does the file behave like i) a sqlite database ii) containing a table 'backbone'
            try:
                # can connect sqlite to any existing path, but only attempts to query it
                # throw up exceptions
                conn = sqlite3.connect(gbif_database)
                _ = conn.execute('select count(*) from backbone;')
            except sqlite3.OperationalError:
                LOGGER.info('Local GBIF database does not contain the backbone table, defaulting '
                            'to online API')
                self.use_local_gbif = False
                self.gbif_conn = None
            except sqlite3.DatabaseError:
                LOGGER.info('Local SQLite database not valid, defaulting to online API')
                self.use_local_gbif = False
                self.gbif_conn = None
            else:
                LOGGER.info('Using local GBIF database to validate taxonomy')
                self.use_local_gbif = True
                self.gbif_conn = conn
                self.gbif_conn.row_factory = sqlite3.Row

    @staticmethod
    def report():
        """
        Static method to return the logging report stored in the global LOG StringIO
        Returns:
            A StringIO containing the logging report.
        """
        return LOG

    # Utility method to update extents
    def update_extent(self, extent, val_type, which):
        """
        Updates one of the three dataset extents
        Args:
            extent: A two tuple (min, max) for one of the extents
            val_type: The type that the values in the extent should be
            which: Which extent to update
        """

        if (not isinstance(extent, tuple) or
                len(extent) != 2 or
                not all([isinstance(vl, val_type) for vl in extent]) or
                extent[0] > extent[1]):
            raise ValueError('extent must be an increasing two-tuple of values of '
                             'type {}'.format(val_type))

        current_vals = getattr(self, which)
        if current_vals is None:
            setattr(self, which, extent)
        else:
            extent = zip(current_vals, extent)
            setattr(self, which, (min(extent[0]), max(extent[1])))

    # Main methods to populate class attributes
    def load_summary(self, validate_doi=False, project_id=None):

        """
        Checks the information in the summary worksheet and looks for the metadata and
        dataset worksheets. The function is intended to try and get as much information
        as possible from the worksheet: the dictionary of metadata returned will have
        None for any missing data, which should be handled by downstream code.

        Args:
            validate_doi: Check any publication DOIs, requiring a web connection.
            project_id: If provided, a list of integer values that are permitted in
                the Project ID field (usually one for a new dataset but more if a 
                published dataset is associated with multiple projects and any of 
                those ids would be valid).
        """

        # try and get the summary worksheet
        LOGGER.info("Checking Summary worksheet",
                    extra={'indent_before': 0, 'indent_after': 1})
        start_errors = CH.counters['ERROR']

        try:
            worksheet = self.workbook.sheet_by_name('Summary')
        except xlrd.XLRDError:
            LOGGER.error("Summary worksheet not found, moving on.")
            return None

        # load worksheet rows
        rows = list(worksheet.get_rows())
        ncols = worksheet.ncols

        # convert into dictionary using the lower cased first entry as the key
        if {rw[0].ctype for rw in rows} != {xlrd.XL_CELL_TEXT}:
            LOGGER.error('Summary metadata fields column contains non text values')

        summary = {unicode(rw[0].value).lower(): rw[1:] for rw in rows}

        # Now define the metadata that may be present. The dictionary entries are
        # 3-tuples with the first entry describing blocks of fields, the second
        # setting if that block is mandatory and the third giving a block title for
        # the logger. Each entry in a block description provides the field name that
        # appears in the summary sheet, if that field is mandatory within the set and
        # an internal field name, if needed in the code or by Zenodo.

        fields = dict(core=([('safe project id', True, 'pid'),
                             ('access status', True, 'access'),
                             ('embargo date', False, 'embargo_date'),
                             ('title', True, None),
                             ('description', True, None)],
                            True, 'Core fields'),
                      keywords=([('keywords', True, None)],
                                True, 'Keywords'),
                      doi=([('publication doi', True, None)],
                           False, 'DOI'),
                      date=([('start date', True, None),
                             ('end date', True, None)],
                            False, 'Date Extents'),
                      geo=([('west', True, None),
                            ('east', True, None),
                            ('south', True, None),
                            ('north', True, None)],
                           False, 'Geographic Extents'),
                      authors=([('author name', True, 'name'),
                                ('author affiliation', False, 'affiliation'),
                                ('author email', False, 'email'),
                                ('author orcid', False, 'orcid')],
                               True, 'Authors'),
                      funding=([('funding body', True, 'body'),
                                ('funding type', True, 'type'),
                                ('funding reference', False, 'ref'),
                                ('funding link', False, 'url')],
                               False, 'Funding Bodies'),
                      external=([('external file', True, 'file'),
                                 ('external file description', True, 'description')],
                                False, 'External Files'),
                      worksheet=([('worksheet name', True, 'name'),
                                  ('worksheet title', True, 'title'),
                                  ('worksheet description', True, 'description'),
                                  ('worksheet external file', False, 'external')],
                                 True, 'Worksheets'),
                      permits=([('permit type', True, 'type'),
                                ('permit authority', True, 'authority'),
                                ('permit number', True, 'number')],
                               False, 'Permits'))

        # Check the minimal keys are expected - mandatory fields in mandatory blocks
        required_blocks = (blk[0] for blk in fields.values() if blk[1])
        required = {fld[0] for blk in required_blocks for fld in blk if fld[1]}

        found = set(summary.keys())

        if not found.issuperset(required):
            LOGGER.error('Missing mandatory metadata fields: ', extra={'join': required - found})

        # Check only valid keys are found
        valid_blocks = (blk[0] for blk in fields.values())
        valid_fields = {fld[0] for blk in valid_blocks for fld in blk}

        if found - valid_fields:
            LOGGER.error('Unknown metadata fields: ', extra={'join': found - valid_fields})

        # Function to read blocks of rows
        def read_block(block):
            """
            Internal function, expecting to run in environment of load_summary.
            Takes a block of rows (mandatory or optional) and returns a list
            of dictionary records, containing xlrd.Cell information so that
            block specific validation can check cells types.

            Args:
                block (3 tuple): An entry from the fields dict giving the fields
                    within the block, whether the block is mandatory and the display
                    title
            Returns:
                None or a list of records.
            """

            fields, mandatory, title = block

            mandatory_fields = [f[0] for f in fields if f[1]]
            optional_fields = [f[0] for f in fields if not f[1]]
            field_map = [(f[0], f[2]) for f in fields]

            # Extract the block of fields, filling in missing fields
            if optional_fields:
                fields = mandatory_fields + optional_fields
            else:
                fields = mandatory_fields

            block = {k: summary[k] if k in summary else [None] * (ncols - 1) for k in fields}

            # Set empty cells to None - ignore the cell type 'empty' here and
            # test the cell value directly to catch pure whitespace content
            for k in block.keys():
                block[k] = [None if dt is None or is_blank(dt.value) else dt for dt in block[k]]

            # Pivot to dictionary of records
            block = [dict(zip(block.keys(), vals)) for vals in zip(*block.values())]

            # Drop empty records
            block = [bl for bl in block if any(bl.values())]

            # Reporting
            if not block:
                if mandatory:
                    LOGGER.error('No {} metadata found'.format(title))
                    return None
            else:
                LOGGER.info('Metadata for {} found: {} records'.format(title, len(block)),
                            extra={'indent_before': 1, 'indent_after': 2})

                # report on block fields
                for fld in mandatory_fields:
                    fld_values = [rec[fld] for rec in block]
                    if not all(fld_values):
                        LOGGER.error('Missing metadata in mandatory field {}'.format(fld))

                # remap names if provided
                to_rename = (mp for mp in field_map if mp[1] is not None)
                for (old, new) in to_rename:
                    for rec in block:
                        rec[new] = rec[old]
                        rec.pop(old)

                return block

        def strip_ctypes(block):
            """
            Converts the xlrd.Cell data in a block to pure values in place
            Args:
                block: A list of dictionary records

            Returns:
                None
            """

            for bl in block:
                bl.update(dict(zip(bl.keys(), [None if b is None else b.value for b in bl.values()])))

        # Now check singleton rows
        singletons = read_block(fields['core'])
        if len(singletons) > 1:
            LOGGER.error('Multiple values found in core field rows')

        singletons = singletons[0]

        # Project ID specific validation
        pid = singletons['pid']

        # check the value is an integer
        if pid is not None:
            if ((pid.ctype != xlrd.XL_CELL_NUMBER) or not pid.value.is_integer()):
                LOGGER.error('SAFE Project ID is not an integer')
            else:
                pid = int(pid.value)

                if project_id is not None and pid not in project_id:
                    pid_str = ', '.join([str(p) for p in project_id])
                    LOGGER.error('SAFE Project ID in file ({}) does not match any '
                                 'provided project ids ({})'.format(pid, pid_str))
                else:
                    self.project_id = pid

        # Title validation
        if singletons['title'] is not None:
            if is_blank(singletons['title'].value):
                LOGGER.error('Dataset title is blank')
            else:
                self.title = singletons['title'].value

        # Description validation
        if singletons['description'] is not None:
            if is_blank(singletons['description'].value):
                LOGGER.error('Dataset description is blank')
            else:
                self.description = singletons['description'].value

        # Access status and embargo validation
        if singletons['access'] is not None:
            access = singletons['access'].value
            if singletons['access'].ctype != xlrd.XL_CELL_TEXT:
                LOGGER.error('Access status not a text value: {}'.format(access))
            elif access.lower() not in ['open', 'embargo', 'closed']:
                LOGGER.error('Access status must be Open, Embargo or Closed '
                             'not {}'.format(access))
            else:
                self.access = access.lower()

                if self.access == 'embargo' and singletons['embargo_date'] is None:
                    LOGGER.error('Dataset embargoed but no embargo date provided')
                elif self.access == 'embargo':
                    embargo_date = singletons['embargo_date'].value
                    if singletons['embargo_date'].ctype != xlrd.XL_CELL_DATE:
                        LOGGER.error('Embargo date not a date value: {}'.format(embargo_date))
                    else:
                        embargo_date = xlrd.xldate_as_datetime(embargo_date, self.workbook.datemode)
                        now = datetime.datetime.now()

                        if embargo_date < now:
                            LOGGER.error('Embargo date is in the past.')
                        elif embargo_date > now + datetime.timedelta(days=2 * 365):
                            LOGGER.error('Embargo date more than two years in the future.')
                        else:
                            self.embargo_date = embargo_date.date().isoformat()
                            LOGGER.info('Dataset access: embargoed until {}'.format(self.embargo_date),
                                        extra={'indent_before': 1, 'indent_after': 0})
                else:
                    LOGGER.info('Dataset access: {}'.format(self.access),
                                extra={'indent_before': 1, 'indent_after': 0})

        # CHECK KEYWORDS
        keywords = read_block(fields['keywords'])

        # data validation for keywords
        if keywords:
            keywords = [entry['keywords'] for entry in keywords]
            if {k.ctype for k in keywords} != {xlrd.XL_CELL_TEXT}:
                LOGGER.error('Keywords contain non-text values')

            # look for commas
            keywords = [entry.value for entry in keywords]
            if any(RE_CONTAINS_PUNC.search(kw) for kw in keywords):
                LOGGER.error('Put each keyword in a separate cell, do not separate '
                             'keywords using commas or semi-colons')

            self.keywords = keywords

        # CHECK FOR PUBLICATION DOIs
        pub_doi = read_block(fields['doi'])

        # data validation for DOIs
        if pub_doi:
            pub_doi = [entry['publication doi'] for entry in pub_doi]
            if {k.ctype for k in pub_doi} != {xlrd.XL_CELL_TEXT}:
                LOGGER.error('Publication DOIs contain non-text values')

            # look for commas
            pub_doi = [entry.value for entry in pub_doi]
            doi_is_url = [RE_DOI.match(vl) for vl in pub_doi]
            if not all(doi_is_url):
                LOGGER.error('Please provide publication DOIs as a URL: https://doi.org/...')

            if validate_doi:
                for doi, is_doi in zip(pub_doi, doi_is_url):
                    if is_doi:
                        api_call = 'https://doi.org/api/handles/{}'.format(doi[is_doi.end():])
                        r = requests.get(api_call)
                        if r.json()['responseCode'] != 1:
                            LOGGER.error('DOI not found: {}'.format(doi))

        self.publication_doi = pub_doi

        # PROCESS BLOCKS OF CELLS
        # Load the AUTHORS block
        authors = read_block(fields['authors'])

        # Author specific validation
        if authors:
            # simplify to cell values not xlrd.Cells
            strip_ctypes(authors)

            # badly formatted names
            bad_names = [rec['name'] for rec in authors if not RE_NAME.match(rec['name'])]
            if bad_names:
                LOGGER.error('Author name not formatted as last_name, first_names: ',
                             extra={'join': bad_names, 'quote': True})

            # badly formatted emails
            bad_emails = [rec['email'] for rec in authors
                          if not is_blank(rec['orcid']) and not RE_EMAIL.match(rec['email'])]
            if bad_emails:
                LOGGER.error('Email not properly formatted: ',
                             extra={'join': bad_emails, 'quote': True})

            # badly formatted orcids
            bad_orcid = [rec['orcid'] for rec in authors
                         if not is_blank(rec['orcid']) and
                         (not isinstance(rec['orcid'], unicode) or
                          not RE_ORCID.match(rec['orcid']))]
            if bad_orcid:
                LOGGER.error('ORCID not properly formatted: ',
                             extra={'join': bad_orcid, 'quote': True})

        self.authors = authors

        # LOAD EXTERNAL FILES - small datasets will usually be contained
        # entirely in a single Excel file, but where formatting or size issues
        # require external files, then names and descriptions are included in
        # the summary information

        external_files = read_block(fields['external'])

        # external file specific validation
        if external_files:
            # simplify to cell values not xlrd.Cells
            strip_ctypes(external_files)

            bad_names = [RE_CONTAINS_WSPACE.search(exf['file']) for exf in self.external_files]
            if any(bad_names):
                LOGGER.error('External file names must not contain whitespace: ',
                             extra={'join': bad_names, 'quote': True})

        self.external_files = external_files

        # Load the WORKSHEETS block
        dataworksheet_summaries = read_block(fields['worksheet'])

        # Worksheet specific validation
        if dataworksheet_summaries:
            # simplify to cell values not xlrd.Cells
            strip_ctypes(dataworksheet_summaries)

            # - catch inclusion of Taxa and Locations in data worksheets
            cited_sheets = [ws['name'] for ws in dataworksheet_summaries]

            if ('Locations' in cited_sheets) or ('Taxa' in cited_sheets):
                LOGGER.error('Do not include Taxa or Locations metadata sheets in '
                             'Data worksheet details')

                dataworksheet_summaries = [ws for ws in dataworksheet_summaries
                                           if ws['name'] not in ('Locations', 'Taxa')]

        # Check possibly modified set of summaries
        if not dataworksheet_summaries:
            if self.external_files:
                LOGGER.info("Only external file descriptions provided")
            else:
                LOGGER.error("No data worksheets or external files provided - no data.")
        else:
            cited_sheets = {ws['name'] for ws in dataworksheet_summaries}
            # names not in list of sheets
            bad_names = cited_sheets - self.sheet_names
            if bad_names:
                LOGGER.error('Worksheet names not found in workbook: ',
                             extra={'join': bad_names, 'quote': True})
            # existing sheets without description
            extra_names = self.sheet_names - {'Summary', 'Taxa', 'Locations'} - cited_sheets
            if extra_names:
                LOGGER.error('Undocumented sheets found in workbook: ',
                             extra={'join': extra_names, 'quote': True})
            # bad external files
            external_in_sheet = {ws['external'] for ws in dataworksheet_summaries
                                 if ws['external'] is not None}
            if self.external_files:
                external_names = {ex['file'] for ex in self.external_files}
            else:
                external_names = set()

            bad_externals = external_in_sheet - external_names
            if bad_externals:
                LOGGER.error('Worksheet descriptions refer to unreported external files.',
                             extra={'join': bad_externals, 'quote': True})

        self.dataworksheet_summaries = dataworksheet_summaries

        # LOOK FOR FUNDING DETAILS - users provide a funding body and a description
        # of the funding type and then optionally a reference number and a URL

        funders = read_block(fields['funding'])
        if funders:
            strip_ctypes(funders)
        self.funders = funders

        # LOOK FOR PERMIT DETAILS - users provide a permit authority, number and permit type

        permits = read_block(fields['permits'])
        if permits:
            strip_ctypes(permits)
            # validate permit types
            permit_types = {pm['type'].lower() for pm in permits}
            valid_permit_types = {'research', 'export', 'ethics'}
            if not permit_types.issubset(valid_permit_types):
                LOGGER.error('Unknown permit types: ',
                             extra={'join': permit_types - valid_permit_types})

        self.permits = permits

        # CHECK EXTENTS - there are six fields to provide temporal and geographic
        # extents for the data. These two extents are a mandatory component of Gemini
        # Metadata so need to be provided if the worksheets and locations don't
        # populate the extents.

        temp_extent = read_block(fields['date'])

        # temporal extent validation and updating
        if temp_extent:
            if len(temp_extent) > 1:
                LOGGER.error('Multiple values found in date extent rows.')

            dates = temp_extent[0]

            date_field_types = {None if val is None else val.ctype for val in dates.values()}

            if not date_field_types.issubset({xlrd.XL_CELL_DATE, None}):
                LOGGER.error('Start and/or end dates not formatted as dates')
            elif all(dates.values()):
                date_vals = (xlrd.xldate_as_datetime(dates['start date'].value,
                                                     self.workbook.datemode),
                             xlrd.xldate_as_datetime(dates['end date'].value,
                                                     self.workbook.datemode))
                epoch_start = xlrd.xldate_as_datetime(1, self.workbook.datemode)
                if date_vals[0] < epoch_start or date_vals[1] < epoch_start:
                    LOGGER.error('Time not date values found in start and/or end date')
                elif date_vals[0] > date_vals[1]:
                    LOGGER.error('Start date is after end date')
                else:
                    self.update_extent(date_vals, datetime.datetime, 'temporal_extent')

        # Geographic extents
        geo_extent = read_block(fields['geo'])

        if geo_extent:
            if len(geo_extent) > 1:
                LOGGER.error('Multiple values found in geographic extent rows.')

            bbox = geo_extent[0]

            bbox_field_types = {None if val is None else val.ctype for val in bbox.values()}

            if not bbox_field_types.issubset({xlrd.XL_CELL_NUMBER, None}):
                LOGGER.error('Geographic extents not numeric')
            elif all(bbox.values()):

                strip_ctypes(geo_extent)
                valid = self.validate_geo_extent((bbox['west'], bbox['east']), 'longitude')
                if valid:
                    self.update_extent((bbox['west'], bbox['east']), float, 'longitudinal_extent')

                valid = self.validate_geo_extent((bbox['south'], bbox['north']), 'latitude')
                if valid:
                    self.update_extent((bbox['south'], bbox['north']), float, 'latitudinal_extent')

        # summary of processing
        n_errors = CH.counters['ERROR'] - start_errors
        if n_errors > 0:
            LOGGER.info('Summary contains {} errors'.format(n_errors),
                        extra={'indent_before': 1, 'indent_after': 0})
        else:
            LOGGER.info('Summary formatted correctly',
                        extra={'indent_before': 1, 'indent_after': 0})

    def load_locations(self, locations_json=None):

        """
        Attempts to load and check the contents of the Locations worksheet and
        compile the geographic extent of the locations used.

        Args:
            locations_json: A path to a JSON file containing a valid set of location names.
                With the default value of None, the function tries to get this from
                a SAFE project website service.
        Returns:
            Populates the locations field of the Dataset object
        """

        # try and get the locations worksheet
        LOGGER.info("Checking Locations worksheet",
                    extra={'indent_before': 0, 'indent_after': 1})
        start_errors = CH.counters['ERROR']

        try:
            locs_wb = self.workbook.sheet_by_name('Locations')
        except xlrd.XLRDError:
            # No locations is pretty implausible, but still persevere as if
            # they aren't going to be required
            LOGGER.warn("No locations worksheet found - moving on")
            return

        # GET THE GAZETTEER VALIDATION INFORMATION
        loc_payload = None
        if locations_json is None:
            # If no file is provided then try and get locations from the website service
            loc_get = requests.get('https://www.safeproject.net/call/json/get_locations_bbox')
            if loc_get.status_code != 200:
                LOGGER.error('Could not download locations. Use a local json file.')
            else:
                loc_payload = loc_get.json()
        else:
            # try and load the file
            try:
                loc_payload = simplejson.load(file(locations_json))
            except IOError:
                LOGGER.error('Could not load location names from file.')

        # process the payload
        if loc_payload is not None:
            valid_locations = loc_payload['locations']
            aliases = loc_payload['aliases']
        else:
            # create empty dictionaries to continue with validation
            valid_locations = {}
            aliases = {}

        # ITERATE OVER THE WORKSHEET ROWS
        loc_rows = locs_wb.get_rows()

        # Get the field headers:
        # Duplicated headers are a problem because the values in the locations dictionaries get
        # overwritten. Depending on what gets overwritten, this can produce really unpredictable
        # bugs, so just stop here.
        hdrs = [cl.value for cl in loc_rows.next()]
        dupes = duplication([h for h in hdrs if not is_blank(h)])
        if dupes:
            LOGGER.error('Duplicated location sheet headers: ',
                         extra={'join': dupes})
            return

        # Lowercase the headers
        hdrs = to_lowercase(hdrs)

        # Check location names are available
        if 'location name' not in hdrs:
            LOGGER.error('Location name column not found')
            return

        # Convert remaining rows into a list of location dictionaries
        locs = [{ky: cl.value for ky, cl in zip(hdrs, rw)} for rw in loc_rows]

        # strip out any rows that consist of nothing but empty cells
        locs = [row for row in locs if not all(is_blank(vl) for vl in row.values())]

        # Location name cleaning - convert floats to unicode via int to handle fractal point numbers
        for rw in locs:
            if isinstance(rw['location name'], unicode):
                pass
            elif isinstance(rw['location name'], float):
                rw['location name'] = unicode(int(rw['location name']))

        # check for rogue whitespace
        ws_padded = [rw['location name'] for rw in locs if is_padded(rw['location name'])]
        if ws_padded:
            LOGGER.error('Locations names with whitespace padding: ',
                         extra={'join': ws_padded, 'quote': True})
            # clean whitespace padding
            for row in locs:
                row['location name'] = row['location name'].strip()

        # look for duplicates
        dupes = duplication([rw['location name'] for rw in locs])
        if dupes:
            LOGGER.error('Duplicated location names: ',
                         extra={'join': dupes, 'quote': True})

        # VALIDATE LOCATIONS
        # Get existing location names and aliases -new location names must not appear
        # in it and existing locations must.
        existing_loc_names = set(valid_locations.keys() + aliases.keys())

        # Split up old and new if there are there any new ones?
        if 'new' in hdrs:

            # Check the New column is just yes, no
            is_new = to_lowercase([rw['new'] for rw in locs])
            is_new = set(is_new)

            # look for blanks
            if any([val is None for val in is_new]):
                LOGGER.error('New locations field contains blank rows.')

            # check only yes or no entries
            valid_new = {'yes', 'no'}
            if not is_new.issubset(valid_new):
                LOGGER.error('New field contains values other than yes and no: ',
                             extra={'join': is_new - valid_new, 'quote': True})

            # extract the new and old locations
            new_locs = [rw for rw in locs if rw['new'].lower() == 'yes']
            locs = [rw for rw in locs if rw['new'].lower() == 'no']
        else:
            new_locs = None

        # Process new locations if there are any
        if new_locs:
            LOGGER.info('{} new locations reported'.format(len(new_locs)))

            # Type is required - used to indicate the kind of location in the absence
            # of any actual geodata
            if 'type' in hdrs:

                # get lowercase types
                geo_types = set(to_lowercase([vl['type'] for vl in new_locs]))

                # Handle blanks
                if None in geo_types:
                    LOGGER.error('Types for new locations contains blank entries.')
                    geo_types -= {None}

                # Handle unknown geo types
                valid_geotypes = {'point', 'linestring', 'polygon', 'transect', 'area'}
                bad_geo_types = geo_types - valid_geotypes
                if bad_geo_types:
                    LOGGER.error('Unknown location types: ',
                                 extra={'join': bad_geo_types, 'quote': True})
            else:
                LOGGER.error('New locations reported but Type field missing')

            # Look for duplicate names
            duplicates_existing = [rw['location name'] for rw in new_locs
                                   if rw['location name'] in existing_loc_names]

            if duplicates_existing:
                LOGGER.error('New location names duplicate existing names and aliases: ',
                             extra={'join': duplicates_existing})

            # Look for geographic data (even if it is just the explicit statement
            # that none is available using NA)

            if not (('latitude' in hdrs and 'longitude' in hdrs) or 'wkt' in hdrs):
                LOGGER.error('New locations reported: you must provide Lat/Long or WKT')
            else:

                # Check Lat Long and WKT using check_field_geo to validate the values,
                # since this method automatically updates the dataset extents.

                if 'latitude' in hdrs and 'longitude' in hdrs:

                    LOGGER.info('Validating lat / long data', extra={'indent_before': 1, 'indent_after': 2})

                    lats = [vl['latitude'] for vl in new_locs if vl['latitude'] != u'NA']
                    non_blank_lats = [vl for vl in lats if not is_blank(vl)]
                    if len(non_blank_lats) < len(lats):
                        LOGGER.error('Blank latitude values for new locations: use NA.')
                    self.check_field_geo({}, non_blank_lats, which='latitude')

                    longs = [vl['longitude'] for vl in new_locs if vl['longitude'] != u'NA']
                    non_blank_longs = [vl for vl in longs if not is_blank(vl)]
                    if len(non_blank_longs) < len(longs):
                        LOGGER.error('Blank longitude values for new locations: use NA.')
                    self.check_field_geo({}, non_blank_longs, which='longitude')

                if 'wkt' in hdrs:

                    LOGGER.info('Validating WKT data', extra={'indent_before': 1, 'indent_after': 2})

                    bad_wkt = []
                    blank_wkt = 0
                    non_blank_longs = []
                    non_blank_lats = []

                    for this_new_loc in new_locs:

                        # Check for blank, NA or something that might be WKT
                        if is_blank(this_new_loc['wkt']):
                            blank_wkt += 1
                        elif this_new_loc['wkt'] == u'NA':
                            pass
                        else:
                            # Run the potential WKT through the parser
                            try:
                                this_new_geom = wkt.loads(this_new_loc['wkt'])
                            except WKTReadingError:
                                bad_wkt.append(this_new_loc['location name'])
                            else:
                                # Is it a valid 2D geom
                                if not this_new_geom.is_valid or this_new_geom.has_z:
                                    bad_wkt.append(this_new_loc['location name'])
                                # Store the extents to check for sensible coordinates
                                bnds = this_new_geom.bounds
                                non_blank_longs.extend([bnds[0], bnds[2]])
                                non_blank_lats.extend([bnds[1], bnds[3]])

                    self.check_field_geo({}, non_blank_longs, which='longitude')
                    self.check_field_geo({}, non_blank_lats, which='latitude')

                    if bad_wkt:
                        LOGGER.error('WKT information badly formatted, not geometrically valid or 3D: ',
                                     extra={'join': bad_wkt, 'quote': True})

                    if blank_wkt:
                        LOGGER.error('WKT field contains blanks for new locations, use NA')

            # new location names
            new_loc_names = {rw['location name'] for rw in new_locs}
        else:
            new_loc_names = set()

        # Process existing locations if there are any
        if locs:
            LOGGER.info('{} existing locations reported'.format(len(locs)), extra={'indent_before': 1})

            # check names exist
            loc_names = {rw['location name'] for rw in locs}
            unknown = loc_names - existing_loc_names
            if unknown:
                LOGGER.error('Unknown locations found: ',
                             extra={'join': unknown, 'quote': True})

            # are aliases being used?
            aliased = loc_names & set(aliases.keys())
            if aliased:
                LOGGER.warn('Locations aliases used. Maybe change to primary location names: ',
                            extra={'join': aliased})

            # Get the bounding box of known locations and aliased locations
            bbox_keys = (loc_names - (unknown | aliased)) | {aliases[ky] for ky in aliased}

            # get the extents of known unaliased locations
            if bbox_keys:
                bbox = [vl for ky, vl in valid_locations.iteritems() if ky in bbox_keys]
                bbox = zip(*bbox)
                self.update_extent((min(bbox[0]), max(bbox[1])), float, 'longitudinal_extent')
                self.update_extent((min(bbox[2]), max(bbox[3])), float, 'latitudinal_extent')

        else:
            loc_names = set()

        # combine locations into set
        self.locations = loc_names | new_loc_names

        # Create tuples to populate a location index. These have the format (name, new, type, WKT).
        # The WKT can not align completely with the type because the lat lon format only provides
        # a single point, which we represent as a Point() WKT. So you could get Linestring, Point().
        # This is historical - type should have been point, transect, area and is really just a
        # description of what the location is. Then lat/long or WKT can be missing but if present
        # give a point location or a more complex geometry.

        index = [(row['location name'], False, None, None)
                 for row in locs]

        new_index = []

        for this_new_loc in new_locs:
            new_entry = [this_new_loc['location name'], True, this_new_loc['type']]

            if 'wkt' in this_new_loc and this_new_loc['wkt'] != u'NA':
                new_entry.append(this_new_loc['wkt'])
            elif ('latitude' in hdrs and 'longitude' in hdrs) and \
                    ((this_new_loc['latitude'] != u'NA') and (this_new_loc['longitude'] != u'NA')):
                new_entry.append(u'Point({longitude} {latitude})'.format(**this_new_loc))
            else:
                new_entry.append(None)

            new_index.append(new_entry)

        self.location_index = index + new_index

        # summary of processing
        n_errors = CH.counters['ERROR'] - start_errors
        if n_errors > 0:
            LOGGER.info('Locations contains {} errors'.format(n_errors))
        else:
            LOGGER.info('{} locations loaded correctly'.format(len(self.locations)))

    def load_taxa(self):

        """
        Attempts to load and check the content of the Taxa worksheet. The
        method checks that all taxa have a local name and then validates
        taxon names and parent names against the GBIF backbone. It populates
        two things:

            i)  the taxon_names attribute of the dataset, which is just a set of
                names used as a validation list for taxon names used in data worksheets.
            ii) the taxon_index attribute of the dataset, which contains a set
                of tuples recording the full hierarchy of the taxa in the dataset
                for use in dataset searching. Each tuple consists of:

                (gbif_id, gbif_parent_id, canonical_name, taxonomic_rank, status, as_name, as_rank)

            The taxon_index is doing dual-duty here:

            a) It is used to populate the taxonomic coverage section of the dataset
               description. In this case, synonymous entries are dropped and the as_name
               and as_rank are used to qualify the accepted name.
            b) It is used as the rows for a dataset_taxon table to index the taxonomic
               coverage of datasets. The as_name and as_rank are not intended to be
               included in this table.

        Returns:
            Updates the taxon_names and taxon_index attributes of the class instance.
        """

        # try and get the taxon worksheet
        LOGGER.info("Checking Taxa worksheet",
                    extra={'indent_before': 0, 'indent_after': 1})
        start_errors = CH.counters['ERROR']

        try:
            sheet = self.workbook.sheet_by_name('Taxa')
        except xlrd.XLRDError:
            # This might mean that the study doesn't have any taxa, so return an empty
            # set. If the datasets then contain taxonomic names, it'll fail gracefully.
            LOGGER.warn("No taxa worksheet found - assuming no taxa in data for now!")
            return

        # A) SANITIZE INPUTS
        # get and check the headers
        tx_rows = sheet.get_rows()
        hdrs = to_lowercase([cl.value for cl in tx_rows.next()])

        # duplicated headers are a problem in that it will cause values in the taxon
        # dictionaries to be overwritten. Depending on what gets overwritten, this can
        # produce really unpredictable bugs, so just stop here.
        dupes = duplication([h for h in hdrs if not is_blank(h)])
        if dupes:
            LOGGER.error('Duplicated column headers in Taxa worksheet: ',
                         extra={'join': dupes})
            return

        # Load dictionaries of the taxa and strip out any rows that consist of nothing
        # but empty cells
        taxa = [{ky: cl.value for ky, cl in zip(hdrs, rw)} for rw in tx_rows]
        taxa = [row for row in taxa if not all([is_blank(vl) for vl in row.values()])]

        # check number of taxa found and standardise names
        if len(taxa) == 0:
            LOGGER.info('No taxon rows found'.format(len(taxa)))
            return
        else:
            # Remove values keyed to None (blank columns),
            _ = [tx.pop(None) for tx in taxa if None in tx]

        # clean the contents of whitespace padding
        for idx, tx in enumerate(taxa):
            for ky, vl in tx.iteritems():
                if is_padded(vl):
                    LOGGER.error('Whitespace padding for {} in row {}:'
                                 ' {}'.format(ky, idx + 2, repr(vl)))
                    tx[ky] = vl.strip()

        # check which fields are found
        if 'name' not in hdrs:
            # dataset names are not found so can't continue
            LOGGER.error('No name column found - no further checking')
            self.taxon_names = set()
            return
        else:
            # Validate the names used within the data
            # Any missing names?
            if any(is_blank(tx['name']) for tx in taxa):
                LOGGER.error('Blank entries in name column')

            taxon_names = [tx['name'] for tx in taxa if not is_blank(tx['name'])]

            # Any duplication in cleaned names
            dupes = duplication(taxon_names)
            if dupes:
                LOGGER.error('Duplicated names found: ', extra={'join': dupes})

            # set the unique taxon names for the dataset instance
            self.taxon_names = set(taxon_names)

            # check to see if the headers are present to validate against GBIF
            if 'taxon name' not in hdrs or 'taxon type' not in hdrs:
                LOGGER.error('At least the taxon type and taxon name columns are required'
                             'for taxon verification')
                return

            # Standardize the taxon representation into a three tuple:
            # (name, (taxon name, taxon type, taxon id), (parent name, parent type, parent id))

            # The dict.get() method automatically fills in None for missing keys, but still need
            # to be wary of blank entries
            taxa = [(tx['name'],
                     (None if is_blank(tx.get('taxon name')) else tx.get('taxon name'),
                      None if is_blank(tx.get('taxon type')) else tx.get('taxon type'),
                      None if is_blank(tx.get('taxon id')) else tx.get('taxon id')),
                     (None if is_blank(tx.get('parent name')) else tx.get('parent name'),
                      None if is_blank(tx.get('parent type')) else tx.get('parent type'),
                      None if is_blank(tx.get('parent id')) else tx.get('parent id')))
                    for tx in taxa]

        # B) VALIDATE TAXONOMY
        # Initialise a set to record failed validation and the set of taxonomic levels
        # that are available to validate in the backbone. See README.md for details of
        # the validation process

        # taxon and parent types that can be checked against gbif and a set of
        # alternative types that will not have a taxon name
        backbone_types = BACKBONE_TYPES
        alt_types = ['morphospecies', 'functional group']

        # Keep a set of taxonomic hierarchy entries to validate after taxon processing
        taxon_hierarchy = set()

        # Check parents first, populating a dictionary keyed on unique parent tuple to
        # record the status of checked parents along with parent gbif information if valid
        parents = set([tx[2] for tx in taxa if not all(is_blank(vl) for vl in tx[2])])
        parent_status = dict()

        if len(parents):
            LOGGER.info('Checking {} parent taxa'.format(len(parents)),
                        extra={'indent_after': 2})

        for prnt in parents:

            # Sanitize inputs. Only two patterns of taxon provision are valid:
            #   name + type + id and name + type
            provided = [not is_blank(p) for p in prnt]
            prnt_string = ', '.join([str(pi) for pi, pr in zip(prnt, provided) if pr])

            # initialise the status with invalid and no information
            parent_status[prnt] = ('invalid', None)

            if provided not in [[True, True, False], [True, True, True]]:
                LOGGER.error('{}: incomplete information'.format(prnt_string))
                continue

            # Standard taxon type?
            if prnt[1].lower() not in backbone_types:
                LOGGER.error('{}: type is not recognized'.format(prnt_string))
                continue

            # Check ID is an integer (xlrd will load as float)
            if prnt[2] is None:
                pass
            elif isinstance(prnt[2], float) and prnt[2].is_integer():
                prnt = (prnt[0], prnt[1], int(prnt[2]))
            else:
                LOGGER.error('{}: GBIF ID is not an integer.'.format(prnt_string))
                continue

            # Now validate against GBIF
            if self.gbif_conn is None:
                prnt_info = web_gbif_validate(prnt[0], prnt[1], prnt[2])
            else:
                prnt_info = local_gbif_validate(self.gbif_conn, prnt[0], prnt[1], prnt[2])

            # handle lookup errors
            if prnt_info['status'] == 'validation_fail':
                LOGGER.error('{}: validation failed'.format(prnt_string))
            elif prnt_info['status'] == 'unknown_id':
                LOGGER.error('{}: GBIF ID not known'.format(prnt_string))
            elif prnt_info['status'] == 'id_mismatch':
                LOGGER.error('{}: ID does not match name and rank'.format(prnt_string))
            elif prnt_info['status'] == 'no_match':
                if 'note' in prnt_info:
                    LOGGER.error('{}: {}'.format(prnt_string, prnt_info['note']))
                else:
                    LOGGER.error('{}: name and rank combination not found'.format(prnt_string))
            else:
                parent_status[prnt] = ('valid', prnt_info)
                # add parent into taxon index
                if 'user' in prnt_info:
                    LOGGER.warn('{}: parent considered a {} of {} in GBIF '
                                'backbone'.format(prnt_string, prnt_info['user'][4],
                                                  prnt_info['canon'][2]))
                    self.taxon_index.add(prnt_info['user'] + (None, None))
                    self.taxon_index.add(prnt_info['canon'] + prnt_info['user'][2:4])
                else:
                    LOGGER.info('{}: parent found'.format(prnt_string))
                    self.taxon_index.add(prnt_info['canon'] + (None, None))

        # Now check main taxa
        LOGGER.info('Validating {} taxa'.format(len(taxa)),
                    extra={'indent_before': 1, 'indent_after': 2})

        for idx, (nm, tx, pr) in enumerate(taxa):

            # row index
            rw_num = idx + 2

            # Sanitize inputs. Only two patterns of taxon provision are valid:
            #   name + type + id and name + type
            provided = [not is_blank(vl) for vl in tx]

            if provided not in [[True, True, False], [True, True, True]]:
                LOGGER.error('Row {} ({}): incomplete information'.format(rw_num, nm))
                continue

            # The taxon input is now sanitized of obvious problems so can be validated against
            # the GBIF backbone database. Validate _all_ backbone type entries: this is to make
            # sure that valid taxa that have had parents provided get indexed correctly. It is
            # only an issue when taxa are not found and also have an invalid parent

            # Check ID is an integer (xlrd will load as float)
            if tx[2] is None:
                pass
            elif isinstance(tx[2], float) and tx[2].is_integer():
                tx = (tx[0], tx[1], int(tx[2]))
            else:
                LOGGER.error('Row {} ({}): GBIF ID is not an integer.'.format(rw_num, nm))
                continue

            if tx[1].lower() not in backbone_types:
                gbif_info = {'status': 'non-backbone'}
            elif self.gbif_conn is None:
                gbif_info = web_gbif_validate(*tx)
            else:
                gbif_info = local_gbif_validate(self.gbif_conn, *tx)

            # Handle immediately problematic lookup errors, leaving found and no_match
            # for cross checking against parent information
            if gbif_info['status'] == 'validation_fail':
                LOGGER.error('Row {} ({}): validation failed'.format(rw_num, nm))
                continue
            elif gbif_info['status'] == 'unknown_id':
                LOGGER.error('Row {} ({}): GBIF ID not known'.format(rw_num, nm))
                continue
            elif gbif_info['status'] == 'id_mismatch':
                LOGGER.error('Row {} ({}): ID does not match name and '
                             'rank'.format(rw_num, nm))
                continue
            else:
                tax_status = gbif_info['status']

            # Lookup the parent status and information
            if pr == (None, None, None):
                par_status, parent_info = (None, None)
            else:
                par_status, parent_info = parent_status[pr]

            # Check the combinations of taxon and parent status. At this point
            # parents are one of found, no_match or None and taxa are one of
            # non-backbone, found or no_match.

            # The combinations are shown below. The taxon row is valid (O) for: a found
            # taxon (with or without a valid parent); a non-matching taxon with a valid
            # parent; and a non-backbone taxon type with a valid parent.

            # Everything else is invalid (X), including a found taxon with a valid parent
            # that isn't actually a parent of the child taxon - hence the question mark
            # in the table.

            #                | None  | pr_inv | pr_val |
            # tx_found       |  O a) |  X b)  |  ? c)  |
            # tx_nomatch     | [    X d)    ] |  O e)  |
            # tx_nonbackbone | [    X f)    ] |  O g)  |

            if tax_status == 'found' and par_status is None:
                # a) Good backbone with no parent, provide info on taxon status
                if 'user' in gbif_info:
                    LOGGER.warn('Row {} ({}): considered a {} of {} in GBIF '
                                'backbone'.format(rw_num, nm, gbif_info['user'][4],
                                                  gbif_info['canon'][2]))
                    self.taxon_index.add(gbif_info['user'] + (None, None))
                    self.taxon_index.add(gbif_info['canon'] + gbif_info['user'][2:4])

                else:
                    LOGGER.info('Row {} ({}): in GBIF backbone '
                                '({})'.format(rw_num, nm, gbif_info['canon'][4]))
                    self.taxon_index.add(gbif_info['canon'] + (None, None))

                # update hierarchy
                taxon_hierarchy.update(gbif_info['hier'])

            elif tax_status == 'found' and par_status == 'invalid':
                # b) Good backbone with bad parent
                LOGGER.error('Row {} ({}): found but provided parent information is '
                             'not valid.'.format(rw_num, nm))

            elif tax_status == 'found' and par_status == 'valid':
                # c) Good backbone with good parent - are they compatible? Check if all
                #    entries in the parent hierarchy appear in the taxon hierarchy
                parent_hier = set(parent_info['hier'])
                taxon_hier = set(gbif_info['hier'])
                if not set(parent_hier).issubset(taxon_hier):
                    LOGGER.error('Row {} ({}): found in GBIF backbone, but additional parent '
                                 'information is incompatible'.format(rw_num, nm))
                else:
                    if 'user' in gbif_info:
                        LOGGER.warn('Row {} ({}): considered a {} of {} in GBIF '
                                    'backbone'.format(rw_num, nm, gbif_info['user'][4],
                                                      gbif_info['canon'][2]))

                        # Add user and canonical usage, including as_name and as_rank
                        self.taxon_index.add(gbif_info['user'] + (None, None))
                        self.taxon_index.add(gbif_info['canon'] + gbif_info['user'][2:4])
                    else:
                        LOGGER.info('Row {} ({}): in GBIF backbone '
                                    '({})'.format(rw_num, nm, gbif_info['canon'][4]))
                        self.taxon_index.add(gbif_info['canon'] + (None, None))

                    taxon_hierarchy.update(gbif_info['hier'])

            elif tax_status == 'no_match' and par_status is None:
                # d) Taxon is a backbone type but is not found in GBIF.
                if 'note' in gbif_info:
                    LOGGER.error('Row {} ({}): {}'.format(rw_num, nm, gbif_info['note']))
                else:
                    LOGGER.error('Row {} ({}): name and rank combination '
                                 'not found'.format(rw_num, nm))

            elif tax_status == 'no_match' and par_status == 'invalid':
                # d) Taxon is a backbone type not found in GBIF and the provided parent isn't valid
                LOGGER.error('Row {} ({}): not found in GBIF and has invalid parent '
                             'information.'.format(rw_num, nm))

            elif tax_status == 'no_match' and par_status == 'valid':
                # e) Taxon is a backbone type that is not present in GBIF but the user has provided
                #    a valid set of parent taxon information.
                LOGGER.info('Row {} ({}): not found in GBIF but has valid parent '
                            'information'.format(rw_num, nm))

                # construct a taxon index entry and add the parent hierarchy
                self.taxon_index.add((-1, parent_info['canon'][0], tx[0],
                                      tx[1].lower(), 'user', None, None))
                taxon_hierarchy.update(parent_info['hier'])

            elif tax_status == 'non-backbone' and (par_status is None or par_status == 'invalid'):
                # f) Taxon is a non-backbone type - must have a valid parent to be accepted.
                LOGGER.error('Row {} ({}): taxa of type {} must have valid parent '
                             'information.'.format(rw_num, nm, tx[1]))

            elif tax_status == 'non-backbone' and par_status == 'valid':
                # g) Taxon is a non backbone type with good parent info
                LOGGER.info("Row {} ({}): {} with valid parent "
                            "information ".format(rw_num, nm, tx[1]))

                # construct the taxon index - use the name for alternative types,
                # otherwise use the taxon name
                if tx[1].lower() in alt_types:
                    taxon_entry = (-1, parent_info['canon'][0], nm,
                                   tx[1].lower(), 'user', None, None)
                else:
                    taxon_entry = (-1, parent_info['canon'][0], nm,
                                   tx[1].lower(), 'user', None, None)

                self.taxon_index.add(taxon_entry)
                taxon_hierarchy.update(parent_info['hier'])

            else:
                # Think all the combinations are covered but exit with information if not
                raise AttributeError('Bad taxon processing')

        # Look up the unique taxon hierarchy entries
        # - drop taxa already in the index
        indexed = [tx[0] for tx in self.taxon_index]
        taxon_hierarchy = {tx for tx in taxon_hierarchy if tx[1] not in indexed}

        # - sort into ascending taxonomic order
        taxon_hierarchy = list(taxon_hierarchy)
        taxon_hierarchy.sort(key=lambda val: backbone_types.index(val[0]))
        LOGGER.info('Indexing taxonomic hierarchy', extra={'indent_before': 1, 'indent_after': 2})

        # Look up the taxonomic hierarchy
        for tx_lev, tx_id in taxon_hierarchy:
            if self.gbif_conn is not None:
                canon = local_gbif_validate(self.gbif_conn, None, None, gbif_id=tx_id)['canon']
            else:
                canon = web_gbif_validate(None, None, gbif_id=tx_id)['canon']

            self.taxon_index.add(canon + (None, None))
            LOGGER.info('Added {}'.format(canon[2]))

        # summary of processing
        n_errors = CH.counters['ERROR'] - start_errors
        if n_errors > 0:
            LOGGER.info('Taxa contains {} errors'.format(n_errors),
                        extra={'indent_before': 1})
        else:
            LOGGER.info('{} taxa loaded correctly'.format(len(self.taxon_names)),
                        extra={'indent_before': 1})

    def load_data_worksheet(self, sheet_meta):

        """
        A method to load and check the formatting and content of a
        data worksheet, turning the dictionary for this worksheet
        in the dataset object into a DataWorksheet object.

        Args:
            sheet_meta: A reference to the metadata dictionary for this
            worksheet in the datasets attribute, containing the
            worksheet name, title and description from the summary and
            the name of any external file to be associated with this.
        """

        # Create a dataworksheet to store details: basically
        # just a dictionary with dot notation and defaults.
        dwsh = DataWorksheet(sheet_meta)
        start_errors = CH.counters['ERROR']

        # For sheet meta with an external file, add in the external file name
        if not is_blank(sheet_meta['external']):
            dwsh.external = sheet_meta['external']

        # Now match to sheet names
        if sheet_meta['name'] not in self.sheet_names and dwsh.external is not None:
            LOGGER.info('Worksheet summary {name} recognized as placeholder for '
                        'external file {external}'.format(**sheet_meta),
                        extra={'indent_before': 0, 'indent_after': 1})
            self.dataworksheets.append(dwsh)
            return
        elif sheet_meta['name'] not in self.sheet_names:
            LOGGER.error('Data worksheet {} not found'.format(sheet_meta['name']),
                         extra={'indent_before': 0, 'indent_after': 1})
            return

        LOGGER.info('Checking data worksheet {}'.format(sheet_meta['name']),
                    extra={'indent_before': 0, 'indent_after': 1})

        # get the worksheet and data dimensions
        worksheet = self.workbook.sheet_by_name(dwsh.name)
        dwsh.max_col = worksheet.ncols
        dwsh.max_row = worksheet.nrows

        # trap completely empty worksheets
        if dwsh.max_row == 0:
            LOGGER.error('Worksheet is empty')
            return

        # get the metadata field names and row numbers - automatically finding
        # the bounds of a worksheet is unreliable so mandatory row numbering is
        # used to make it explicit
        first_column = worksheet.col_values(0)
        first_column_types = worksheet.col_types(0)

        # The types should be a sequence [xlrd.XL_CELL_TEXT, ..., xlrd.XL_CELL_NUMBER, ..., ? Blank cells]
        first_number_index = first_column_types.index(xlrd.XL_CELL_NUMBER)

        dwsh.descriptors = first_column[:first_number_index]
        row_numbers = first_column[first_number_index:]
        row_types = first_column_types[first_number_index:]

        # Handle descriptor parsing errors
        if len(dwsh.descriptors) == 0:
            LOGGER.error('Cannot parse data: no descriptors found')
            return
        elif 'field_name' not in dwsh.descriptors:
            LOGGER.error('Cannot parse data: field_name row not found')
            return
        elif 'field_name' != dwsh.descriptors[-1]:
            LOGGER.error('Cannot parse data: field_name row is not the last descriptor')
            return
        else:
            dwsh.field_name_row = first_number_index

        # check the row numbering makes sense, allowing for special case where
        # table descriptions for external files have no data, just headers
        if len(row_numbers) > 0:
            # Check for expected row numbering
            # - make a special case for terminal blanks, which might just have
            #   whitespace junk in cells or have been hung on to by Excel.
            n_terminal_blanks = 0
            while is_blank(row_numbers[-1]):
                row_numbers.pop(-1)
                n_terminal_blanks += 1

            # See if terminal blanks are actually empty
            if n_terminal_blanks:
                row_types = row_types[: -n_terminal_blanks]
                row_range = range(dwsh.max_row - 1, (dwsh.max_row - 1) - n_terminal_blanks, - 1)
                for check_row in row_range:
                    if all(is_blank(val) for val in worksheet.row_values(check_row)):
                        dwsh.max_row -= 1
                    else:
                        LOGGER.error('Un-numbered rows at end of worksheet contain data')
                        break

            # Check what is left
            if set(row_types) == {xlrd.XL_CELL_NUMBER}:
                ideal = [float(i) for i in range(1, len(row_numbers) + 1)]
                if row_numbers != ideal:
                    # All numbers but not the right ones
                    LOGGER.error("Row numbers not consecutive or do not start with 1")

        elif is_blank(sheet_meta['external']):
            LOGGER.error('No data found below field descriptors.')
        else:
            LOGGER.info('Data table description associated with '
                        'external file {external}'.format(**sheet_meta))

        # report on detected size
        dwsh.n_data_row = dwsh.max_row - dwsh.field_name_row
        LOGGER.info('Worksheet contains {n_d} descriptors, {o.n_data_row} data rows and '
                    '{n_f} fields'.format(o=dwsh, n_d=len(dwsh.descriptors), n_f=dwsh.max_col - 1),
                    extra={'indent_after': 2})

        # extract dictionaries of metadata per field, setting blank entries to None
        metadata = [[None if is_blank(val) else val
                     for val in worksheet.col_values(cl, 0, dwsh.field_name_row)]
                    for cl in range(1, dwsh.max_col)]
        metadata = [dict(zip(dwsh.descriptors, fld)) for fld in metadata]

        # insert column index
        for idx, fld in enumerate(metadata):
            fld[u'col_idx'] = idx + 1

        # check field names unique (drop None). This doesn't cause as many problems
        # as duplications in Taxa and Locations, which expect certain fields, so warn
        # and continue.
        field_names = [fld['field_name'] for fld in metadata if fld['field_name'] is not None]
        dupes = duplication(field_names)
        if dupes:
            LOGGER.error('Field names duplicated: ', extra={'join': dupes})

        # get taxa field names for cross checking observation and trait data
        dwsh.taxa_fields = [fld['field_name'] for fld in metadata if fld['field_type'] == 'Taxa']

        # check the data in each field against the metadata
        for meta in metadata:
            # read the values and check them against the metadata
            data = worksheet.col(meta['col_idx'], dwsh.field_name_row, dwsh.max_row)
            self.check_field(dwsh, meta, data)

        # add the new DataWorksheet into the Dataset
        self.dataworksheets.append(dwsh)

        # reporting
        n_errors = CH.counters['ERROR'] - start_errors
        if n_errors > 0:
            LOGGER.info('Dataframe contains {} errors'.format(n_errors),
                        extra={'indent_before': 1})
        else:
            LOGGER.info('Dataframe formatted correctly',
                        extra={'indent_before': 1})

    def check_field(self, dwsh, meta, data):
        """
        Method to test whether the contents of a field are compatible
        with the metadata for the field. Largely used for checking
        data worksheets, but also by other methods. Acts as a wrapper
        around data type specific subfunctions.

        Args:
            dwsh: A DataWorksheet, used to pass worksheet level information,
             which at the moment is just taxa fields.
            meta: A dictionary of metadata
            data: A list of values
        """

        # Print out column checking header
        if is_blank(meta['field_name']):
            LOGGER.info('Checking Column {}'.format(xlrd.colname(meta['col_idx'])),
                        extra={'indent_before': 2, 'indent_after': 3})
        else:
            # sanitize and check field name - aiming for ascii compliant with no padding
            fld_name = meta['field_name']
            fld_name_ascii = fld_name.encode('ascii', 'ignore')

            LOGGER.info('Checking field {}'.format(fld_name_ascii),
                        extra={'indent_before': 2, 'indent_after': 3})

            if is_padded(fld_name):
                LOGGER.error('Field name contains white space padding')
            if len(fld_name) > len(fld_name_ascii):
                LOGGER.error('Field name contains non ASCII characters')

        # Skip any field with no user provided metadata or data
        blank_data = [is_blank(cl.value) for cl in data]
        blank_meta = [is_blank(vl) for ky, vl in meta.iteritems()
                      if ky not in ['col_idx', 'column']]
        if all(blank_data) and all(blank_meta):
            LOGGER.info('Blank column loaded - safe to ignore')
            return
        elif all(blank_meta):
            LOGGER.error('Field contains no descriptor information but does contain values')
            return

        # try and figure out what else is available
        if is_blank(meta['field_name']):
            LOGGER.error('Field name is blank')

        # check the description
        if is_blank(meta['description']):
            LOGGER.error('Description is missing')

        # Test explicitly lower cased field type  values to avoid annoying users
        # look for strip white space
        field_type = to_lowercase([meta['field_type']])[0]

        # check for padding in field type
        if is_padded(field_type):
            LOGGER.error('Field type contains white space padding: ',
                         extra={'join': [field_type], 'quote': True})
            field_type = field_type.strip()

        # filter out missing and blank data, except for comments fields, where
        # blanks are not an error. Also check for Excel formula errors
        if field_type != 'comments':

            # Look for NAs and Excel errors
            na_vals = [dt.value == u'NA' for dt in data]
            xl_error = [dt.ctype == xlrd.XL_CELL_ERROR for dt in data]

            if any(na_vals):
                LOGGER.warn('{} / {} values missing'.format(sum(na_vals), len(na_vals)))

            # We won't tolerate blank data
            if any(blank_data):
                LOGGER.error('{} cells are blank or contain only whitespace '
                             'text'.format(sum(blank_data)))

            if any(xl_error):
                LOGGER.error('{} cells contain Excel # error codes'.format(sum(xl_error)))

            # Return the values that aren't NA, blank, whitespace or errors
            drop = [any(tests) for tests in zip(na_vals, blank_data, xl_error)]
            data = [dt for dt, nb in zip(data, drop) if not nb]

        # run consistency checks where needed and trap unknown field types
        if field_type == 'date':
            self.check_field_datetime(meta, data, which='date')
        elif field_type == 'datetime':
            self.check_field_datetime(meta, data, which='datetime')
        elif field_type == 'time':
            self.check_field_datetime(meta, data, which='time')
        elif field_type == 'taxa':
            self.check_field_taxa(data)
        elif field_type == 'location':
            self.check_field_locations(data)
        elif field_type in ['categorical', 'ordered categorical']:
            self.check_field_categorical(meta, data)
        elif field_type == 'numeric':
            self.check_field_numeric(meta, data)
        elif field_type == 'abundance':
            self.check_field_abundance(meta, data, dwsh.taxa_fields)
        elif field_type in ['categorical trait', 'ordered categorical trait']:
            self.check_field_trait(meta, data, dwsh.taxa_fields, which='categorical')
        elif field_type == 'numeric trait':
            self.check_field_trait(meta, data, dwsh.taxa_fields, which='numeric')
        elif field_type in ['categorical interaction', 'ordered categorical interaction']:
            self.check_field_interaction(meta, data, dwsh.taxa_fields, which='categorical')
        elif field_type == 'numeric interaction':
            self.check_field_interaction(meta, data, dwsh.taxa_fields, which='numeric')
        elif field_type == 'latitude':
            # check field geo expects values in data not xlrd.Cell
            data = [dt.value for dt in data]
            self.check_field_geo(meta, data, which='latitude')
        elif field_type == 'longitude':
            # check field geo expects values in data not xlrd.Cell
            data = [dt.value for dt in data]
            self.check_field_geo(meta, data, which='longitude')
        elif field_type == 'file':
            data = [dt.value for dt in data]
            self.check_field_file(meta, data)
        elif field_type in ['replicate', 'id']:
            # We've looked for missing data, no other constraints.
            pass
        elif field_type == 'comments':
            pass
        elif field_type is None:
            LOGGER.error('Field type is empty')
        else:
            LOGGER.error('Unknown field type {field_type}'.format(**meta))

        # extend the dataworksheet fields
        dwsh.fields.append(meta)

    # Helper functions for checking data fields
    @staticmethod
    def _check_meta(meta, descriptor):
        """
        A standardised check to see if a required descriptor is present for
        a field and that it isn't simply empty or whitespace. The function
        reports problems to the Messages instance and returns a boolean
        showing if the checks passed successfully.

        Args:
            meta: A dictionary of field metadata descriptors
            descriptor: The name of the descriptor to check.

        Returns:
            A boolean, with True showing no problems and False showing
            that warnings occurred.
        """

        if descriptor not in meta:
            LOGGER.error('{} descriptor missing'.format(descriptor))
            return False
        elif is_blank(meta[descriptor]):
            LOGGER.error('{} descriptor is blank'.format(descriptor))
            return False
        else:
            return True

    def _check_taxon_meta(self, meta, taxa_fields):

        """
        Checks the taxonomic metadata of abundance and trait fields.
        This is more involved that the simple _check_meta(), because
        of the option to provide taxon_name or taxon_field descriptors

        Args:
            meta: A dictionary of metadata descriptors for the field
            taxa_fields: A list of Taxa fields in this worksheet.

        Returns:
            A boolean, with True showing no problems and False showing
            that warnings occurred.
        """

        # Are taxon_name and taxon_field provided and not blank: note use of
        # 'and' rather than '&' to allow missing descriptors to short cut

        tx_nm_prov = ('taxon_name' in meta) and (not is_blank(meta['taxon_name']))
        tx_fd_prov = ('taxon_field' in meta) and (not is_blank(meta['taxon_field']))

        if tx_nm_prov and tx_fd_prov:
            LOGGER.error('Taxon name and taxon field both provided, use one only')
            return False
        elif tx_nm_prov and meta['taxon_name'] not in self.taxon_names:
            LOGGER.error('Taxon name not found in the Taxa worksheet')
            return False
        elif tx_fd_prov and meta['taxon_field'] not in taxa_fields:
            LOGGER.error("Taxon field not found in this worksheet")
            return False
        elif not tx_nm_prov and not tx_fd_prov:
            LOGGER.error("One of taxon name or taxon field must be provided")
            return False
        else:
            if tx_nm_prov:
                self.taxon_names_used.update([meta['taxon_name']])
            return True

    def _check_interaction_meta(self, meta, taxa_fields):

        """
        Checks the taxonomic metadata of interaction fields.
        This is more involved that the simple _check_meta(), because
        of the option to provide taxon_name or taxon_field descriptors
        describing at least two taxonomic identifiers.

        Args:
            meta: A dictionary of metadata descriptors for the field
            taxa_fields: A list of Taxa fields in this worksheet.

        Returns:
            A boolean, with True showing no problems and False showing
            that warnings occurred.
        """

        # Are interaction_name and/or interaction_field provided and not blank:
        #  note use of 'and' rather than '&' to allow missing descriptors to short cut

        iact_nm_prov = ('interaction_name' in meta) and (not is_blank(meta['interaction_name']))
        iact_fd_prov = ('interaction_field' in meta) and (not is_blank(meta['interaction_field']))

        if not iact_nm_prov and not iact_fd_prov:
            LOGGER.error("At least one of interaction name or interaction field must be provided")
            return False
        else:
            if iact_nm_prov:
                # get the taxon names and descriptions from interaction name providers
                int_nm_lab, int_nm_desc = self._parse_levels(meta['interaction_name'])
                # add names to used taxa
                self.taxon_names_used.update(int_nm_lab)
                # check they are found
                if not all([lab in self.taxon_names for lab in int_nm_lab]):
                    LOGGER.error('Unknown taxa in interaction_name descriptor')
                    nm_check = False
                else:

                    nm_check = True
            else:
                int_nm_lab, int_nm_desc = [(), ()]
                nm_check = True

            if iact_fd_prov:
                # check any field labels match to known taxon fields
                int_fd_lab, int_fd_desc = self._parse_levels(meta['interaction_field'])
                if not all([lab in taxa_fields for lab in int_fd_lab]):
                    LOGGER.error('Unknown taxon fields in interaction_field descriptor')
                    fd_check = False
                else:
                    fd_check = True
            else:
                int_fd_lab, int_fd_desc = [(), ()]
                fd_check = True

            if len(int_nm_lab + int_fd_lab) < 2:
                LOGGER.error('At least two interacting taxon labels or fields must be identified')
                num_check = False
            else:
                num_check = True

            if nm_check and fd_check and num_check:
                return True
            else:
                return False

    @staticmethod
    def _parse_levels(txt):
        """
        Splits up category information formatted as label:desc;label:desc, which
        is used in both levels for categorical data and interaction descriptors.
        Args:
            txt: The text string to parse

        Returns:
            A list of two tuples of label and descriptions.
        """

        # remove terminal semi-colon, if used.
        if txt.endswith(';'):
            txt = txt[:-1]
        # - split the text up by semi-colon
        parts = txt.split(';')
        # - split descriptions
        parts = [pt.split(':') for pt in parts]
        n_parts = [len(pt) for pt in parts]

        # simple formatting checks
        if any([pt > 2 for pt in n_parts]):
            LOGGER.error('Extra colons in level description.')

        # standardise descriptions
        if all([pt == 1 for pt in n_parts]):
            parts = [[pt[0], None] for pt in parts]
        elif all([pt == 2 for pt in n_parts]):
            pass
        else:
            LOGGER.error('Provide descriptions for either all or none of the categories')
            parts = [pt[0:2] if len(pt) >= 2 else [pt[0], None] for pt in parts]

        return zip(*parts)

    @staticmethod
    def validate_geo_extent(extent, which):

        if which == 'latitude':
            bnds = [-90, -4, 8, 90]
        elif which == 'longitude':
            bnds = [-180, 108, 120, 180]

        if extent[0] > extent[1]:
            LOGGER.error('{0}: lower bound greater than upper bound'.format(which.capitalize()))
            return False

        out_of_bounds = extent[0] < bnds[0] or extent[1] > bnds[3]
        out_of_borneo = bnds[0] <= extent[0] < bnds[1] or bnds[2] < extent[1] <= bnds[3]

        if out_of_bounds:
            LOGGER.error('{0} values not in valid range[{1[0]}, {1[3]}]: '
                         '{2}'.format(which.capitalize(), bnds, extent))
            return False
        elif out_of_borneo:
            LOGGER.warn('{0} values not in Borneo [{1[1]}, {1[2]}]: '
                        '{2}'.format(which.capitalize(), bnds, extent))
            return True
        else:
            return True

    def check_field_datetime(self, meta, data, which='datetime'):

        """
        Checks for data consistency in date and datetime fields. Excel
        doesn't distinguish, both are loaded as datetime.datetime objects
        so we check that the values are compatible with the user provided
        field type. Also handle datetimes provided as strings in common
        ISO formats

        Args:
            meta: The field metadata, to be updated with the range
            data: A list of data values, allegedly containing datetimes or dates
            which: The datetime type to be checked for.
        """

        # Check types and allow all xlrd.XL_CELL_DATE or all xlrd.XL_CELL_STRING
        # but not a mix
        cell_types = {dt.ctype for dt in data}
        data = [dt.value for dt in data]

        if cell_types == {xlrd.XL_CELL_DATE}:

            # For date and time options, check decimal components
            if which == 'date':
                contains_time = [dt % 1 > 0 for dt in data]
                if any(contains_time):
                    LOGGER.error('Some values also contain time components')

            elif which == 'time':
                contains_date = [1 <= dt for dt in data]
                if any(contains_date):
                    LOGGER.error('Some values also contain date components')

            # For date containing options, check for actual dates and get extents
            if which in ['date', 'datetime']:
                if any(0.0 <= dt < 1.0 for dt in data):
                    LOGGER.error('Some values _only_  contain time components')

            # get extents
            if len(data):
                extent = (xlrd.xldate_as_datetime(min(data), self.workbook.datemode),
                          xlrd.xldate_as_datetime(max(data), self.workbook.datemode))

                if which == 'time':
                    extent = tuple(vl.time() for vl in extent)
            else:
                extent = None

        elif cell_types == {xlrd.XL_CELL_TEXT}:

            # internal function to check dates
            def parse_datetime(dt, which='date'):
                try:
                    if which == 'time':
                        dt = parser.isoparser().parse_isotime(dt)
                    else:
                        dt = parser.isoparse(dt)

                    return dt, True
                except ValueError:
                    return dt, False

            parsed_data = [parse_datetime(dt, which) for dt in data]
            good_data = set((dt[0] for dt in parsed_data if dt[1]))
            bad_data = set((dt[0] for dt in parsed_data if not dt[1]))

            if len(good_data):
                extent = (min(good_data), max(good_data))
                # intentionally scrub out timezones - Excel dates don't have them and can't
                # compare datetimes with mixed tzinfo status
                extent = tuple(dt.replace(tzinfo=None) for dt in extent)
            else:
                extent = None

            if len(bad_data):
                LOGGER.error('Problem in parsing date/time strings: ', extra={'join': bad_data})

        else:
            LOGGER.error('Field contains data with mixed formatting. Use either Excel date formats or '
                         'ISO formatted text. Note that text can look _exactly_ like an Excel date or '
                         'time cell, you may need to copy the column and format as numbers to spot errors.')

            extent = None

        # range and extent setting
        if extent is not None:
            meta['range'] = extent
            if which in ['date', 'datetime']:
                self.update_extent(extent, datetime.datetime, 'temporal_extent')

    def check_field_taxa(self, data):

        """
        Checks if all the values provided in a Taxon field are found
        in the Taxa worksheet.

        Args:
            data: A list of data values, allegedly taxon names
        """

        found = set(dt.value for dt in data)
        if self.taxon_names == set():
            LOGGER.error('No taxa loaded', 2)
        if not found.issubset(self.taxon_names):
            LOGGER.error('Includes taxa missing from Taxa worksheet: ',
                         extra={'join': found - self.taxon_names, 'quote': True})

        # add the found taxa to the list of taxa used
        self.taxon_names_used.update(found)

    def check_field_locations(self, data):

        """
        Checks if all the values provided in a Locations field are
        found in the Locations worksheet, reporting to the Messages instance.

        Args:
            data: A list of data values, allegedly taxon names
        """

        # location names should be strings but we allow integer point numbers
        data = [unicode(int(dt.value)) if dt.ctype == xlrd.XL_CELL_NUMBER
                else unicode(dt.value) for dt in data]

        # check if locations are all provided
        found = set(data)
        if self.locations == set():
            LOGGER.error('No locations loaded')
        elif not found.issubset(self.locations):
            LOGGER.error('Includes locations missing from Locations worksheet:',
                         extra={'join': found - self.locations, 'quote': True})

        # add the locations to the set of locations used
        self.locations_used.update(found)

    def check_field_abundance(self, meta, data, taxa_fields):

        """
        Checks abundance type data, reporting to the Messages instance.

        Args:
            meta: A dictionary of metadata descriptors for the field
            data: A list of data values, allegedly numeric
            taxa_fields: A list of Taxa fields in this worksheet.
        """

        # check the required descriptors
        self._check_meta(meta, 'method')
        self._check_taxon_meta(meta, taxa_fields)

        # Can still check values are numeric, whatever happens above.
        # We're not going to insist on integers here - could be mean counts.
        nums = [dt.value for dt in data if dt.ctype == xlrd.XL_CELL_NUMBER]

        if len(nums) < len(data):
            LOGGER.error('Field contains non-numeric data')

        # update the field metadata if there is any data.
        if len(nums):
            meta['range'] = (min(nums), max(nums))

    def check_field_categorical(self, meta, data):

        """
        Checks factor data, reporting to the Messages instance.

        Args:
            meta: A dictionary of metadata descriptors for the field
            data: A list of data values, allegedly strings from a provided set of levels
        """

        # Are levels found?
        ct_ok = self._check_meta(meta, 'levels')

        if not ct_ok:
            # Can't check further if no levels descriptor
            pass
        elif ct_ok and not isinstance(meta['levels'], unicode):
            # Can't really check anything here either
            LOGGER.error('Category description does not seem to be text')
        else:
            # Now we can test if the labels match up
            level_labels, level_desc = self._parse_levels(meta['levels'])

            # - repeated labels?
            if len(set(level_labels)) < len(level_labels):
                LOGGER.error('Repeated level labels')

            # - check for numeric level names: integers would be more common
            #   but don't let floats sneak through either!
            integer_codes = [is_numeric_string(vl) for vl in level_labels]
            if any(integer_codes):
                LOGGER.error('Numeric level names not permitted')

            # Remove white space around the labels: simple spacing in the text
            # makes it easier to read and insisting on no space is unnecessary
            level_labels = [vl.strip() for vl in level_labels]

            # Now look for consistency: get the unique values reported in the
            # data, convert to unicode to handle checking of numeric labels and
            # then check the reported levels are a subset of the descriptors.
            # XLRD reads all numbers as floats, so coerce floats back to int
            reported = set(data)
            reported = {lv.value for lv in reported}
            found = set()
            for lv in reported:
                if isinstance(lv, float) and lv.is_integer() and unicode(int(lv)) in level_labels:
                    found.add(lv)
                elif isinstance(lv, float) and unicode(lv) in level_labels:
                    found.add(lv)
                elif unicode(lv) in level_labels:
                    found.add(lv)

            if found != reported:
                LOGGER.error('Categories found in data missing from levels descriptor row: ',
                             extra={'join': reported - found, 'quote': True})

    def check_field_numeric(self, meta, data):

        """
        Checks numeric type data, reporting to the Messages instance.

        Args:
            meta: A dictionary of metadata descriptors for the field
            data: A list of data values, allegedly numeric
        """

        # Check required descriptors
        self._check_meta(meta, 'units')
        self._check_meta(meta, 'method')

        # Regardless of the outcome of the meta checks, can still check the
        # data is all numeric, as it claims to be. Keep dates separate because
        # they are a pain to find if included as their numeric storage value
        good, bad, ugly = [], [], []
        for dt in data:
            if dt.ctype == xlrd.XL_CELL_NUMBER:
                good.append(dt.value)
            elif dt.ctype == xlrd.XL_CELL_DATE:
                ugly.append(dt.value)
            else:
                bad.append(dt.value)

        if bad:
            LOGGER.error('Field contains non-numeric data: ',
                         extra={'join': set(bad), 'quote': True})

        if ugly:
            ugly = [xlrd.xldate_as_datetime(ug, self.workbook.datemode).isoformat() for ug in ugly]
            LOGGER.error('Field contains date/time formatted data - format will differ from '
                         'from the values shown: ', extra={'join': ugly})

        # update the field metadata if there is any data
        if len(good):
            meta['range'] = (min(good), max(good))

    def check_field_trait(self, meta, data, taxa_fields, which='categorical'):

        """
        Checks trait type data and reports to the Messages instance. Just a wrapper
        to check that a valid taxon has been provided before handing off to
        check_field_categorical or check_field_numeric

        Args:
            meta: A dictionary of metadata descriptors for the field
            data: A list of data values, allegedly numeric
            taxa_fields: A list of Taxa fields in this worksheet.
            which: The type of trait data in the field
        """

        # Check required descriptors
        self._check_taxon_meta(meta, taxa_fields)

        # Regardless of the outcome of the meta checks, check the contents:
        if which == 'categorical':
            self.check_field_categorical(meta, data)
        elif which == 'numeric':
            self.check_field_numeric(meta, data)

    def check_field_interaction(self, meta, data, taxa_fields, which='categorical'):

        """
        Checks interaction type data and reports to the Messages instance.
        Just a wrapper to check that intercating taxa have been identified
        before handing off to check_field_categorical or check_field_numeric

        Args:
            meta: A dictionary of metadata descriptors for the field
            data: A list of data values, allegedly numeric
            taxa_fields: A list of Taxa fields in this worksheet.
            which: The type of trait data in the field
        """

        # Check required descriptors
        self._check_interaction_meta(meta, taxa_fields)

        # Regardless of the outcome of the meta checks, check the contents:
        if which == 'categorical':
            self.check_field_categorical(meta, data)
        elif which == 'numeric':
            self.check_field_numeric(meta, data)

    def check_field_geo(self, meta, data, which='latitude'):

        """
        Checks geographic coordinates. It also automatically updates
        the geographic extent of the dataset.

        Args:
            meta: A dictionary of metadata descriptors for the field
            data: A list of data values
            which: One of latitude or longitude
        """

        # Are the values represented as decimal degrees - numeric.
        nums = [dt for dt in data if isinstance(dt, float)]

        if len(nums) < len(data):
            LOGGER.error('Field contains non-numeric data')
            if any([RE_DMS.search(unicode(dt)) for dt in data]):
                LOGGER.warn('Possible degrees minutes and seconds formatting? Use decimal degrees')

        # Check the locations
        if len(nums):
            min_geo = float(min(nums))
            max_geo = float(max(nums))
            extent = (min_geo, max_geo)
            valid = self.validate_geo_extent(extent, which)

            if valid:
                # update the field metadata and the dataset extent
                meta['range'] = extent
                # Look up the extent name to update and then update it
                which_extent = {'latitude': 'latitudinal_extent',
                                'longitude': 'longitudinal_extent'}
                self.update_extent(extent, float, which_extent[which])

    def check_field_file(self, meta, data):
        """
        Checks file fields. The data values need to match to an external file
        or can be contained within an archive file provided in the 'file_container'
        descriptor.

        Args:
            meta: A dictionary of metadata descriptors for the field
            data: A list of data values
        """

        external_names = {ex['file'] for ex in self.external_files}

        if 'file_container' in meta and meta['file_container'] is not None:
            if meta['file_container'] not in external_names:
                LOGGER.error(
                    "Field file_container value not found in external files: {}".format(meta['file_container']))
        else:
            missing_files = set(data) - external_names
            if missing_files:
                LOGGER.error("Field contains files not listed in external files: ",
                             extra={'join': missing_files})

    def final_checks(self):
        """
        A method to run final checks:
        i)  The locations and taxa provided have been used in the data worksheets scanned.
        ii) Report the total number of errors and warnings
        """
        LOGGER.info('Checking provided locations and taxa all used in data worksheets',
                    extra={'indent_before': 0, 'indent_after': 1})

        # check locations - can't validate when there are external files which
        # might use any unused ones.
        if self.locations:
            if self.locations_used == self.locations:
                LOGGER.info('Provided locations all used in datasets')
            elif self.external_files:
                LOGGER.warn('Location list cannot be validated when external data files are used')
            else:
                LOGGER.error('Provided locations not used: ',
                             extra={'join': self.locations - self.locations_used})

        # check taxa
        if self.taxon_names:
            if self.taxon_names_used == self.taxon_names:
                LOGGER.info('Provided taxa all used in datasets')
            elif self.external_files:
                LOGGER.warn('Taxon list cannot be validated when external data files are used')
            else:
                LOGGER.error('Provided taxa  not used: ',
                             extra={'join': self.taxon_names - self.taxon_names_used})

        LOGGER.info('Checking temporal and geographic extents',
                    extra={'indent_before': 0, 'indent_after': 1})
        if self.temporal_extent is None:
            LOGGER.error('Temporal extent not set from data, please add start and '
                         'end dates to summary worksheet')
        if self.latitudinal_extent is None or self.longitudinal_extent is None:
            LOGGER.error('Geographic extent not set from data, please add south, north '
                         'east and west bounds to summary worksheet')

        if CH.counters['ERROR'] > 0:
            if CH.counters['WARNING'] > 0:
                LOGGER.info('FAIL: file contained {} errors and {} '
                            'warnings'.format(CH.counters['ERROR'], CH.counters['WARNING']),
                            extra={'indent_before': 0})
            else:
                LOGGER.info('FAIL: file contained {} errors'.format(CH.counters['ERROR']),
                            extra={'indent_before': 0})
        else:
            if CH.counters['WARNING'] > 0:
                LOGGER.info('PASS: file formatted correctly but with {} '
                            'warnings.'.format(CH.counters['WARNING']),
                            extra={'indent_before': 0})
                self.passed = True
            else:
                LOGGER.info('PASS: file formatted correctly with no warnings',
                            extra={'indent_before': 0})
                self.passed = True

    def export_metadata_dict(self):
        """
        Function to return a simple dictionary of the metadata content, combining
        dataworksheets, into a single representation of the contents

        Returns:
            A dictionary of metadata
        """

        # get the required components
        component_keys = ['access', 'authors', 'description', 'embargo_date', 'filename',
                          'keywords', 'latitudinal_extent', 'longitudinal_extent', 'funders',
                          'project_id', 'temporal_extent', 'title', 'external_files', 'permits']

        components = {ky: vl for ky, vl in self.__dict__.iteritems() if ky in component_keys}

        # add in the data worksheets
        components['dataworksheets'] = [dwsh.__dict__ for dwsh in self.dataworksheets]

        return components


"""
Higher level functions
"""


def check_file(fname, verbose=True, gbif_database=None, locations_json=None, validate_doi=False,
               check='sltwf', project_id=None):
    """
    Runs the format checking across an Excel workbook.

    Parameters:
        fname: Path to an Excel file
        verbose: Boolean to indicate whether to print messages to stdout as the program runs?
        gbif_database: Path to a local copy of the GBIF taxonomy backbone if that is to be used
          instead of the GBIF web API.
        locations_json: The path to a json file of valid location names
        validate_doi: Check any publication DOIs resolve, requiring a web connection.
        check: String indicating which checking to run. Note that running worksheet checking
          without loading summary taxa and locations is going to be problematic!
        project_id: Optional integer value to be checked against the project number included in
          the summary worksheet.

    Returns:
        A Dataset object
    """

    # initialise the dataset object
    dataset = Dataset(fname, verbose=verbose, gbif_database=gbif_database)

    # load the metadata sheets
    if 's' in check:
        dataset.load_summary(validate_doi=validate_doi, project_id=project_id)
    if 't' in check:
        dataset.load_taxa()
    if 'l' in check:
        dataset.load_locations(locations_json=locations_json)

    # check the datasets
    if 'w' in check:
        if dataset.dataworksheet_summaries:
            for ws in dataset.dataworksheet_summaries:
                dataset.load_data_worksheet(ws)
        else:
            LOGGER.error('No data worksheets found')

    if 'f' in check:
        dataset.final_checks()

    return dataset


def main():
    """
    This program validates an Excel file formatted as a SAFE dataset. As it runs, it outputs
    a report that highlights any problems with the formatting.

    Much of the validation is to check that the data meets our metadata standards and is
    internally consistent. However, it uses external sources to perform validation in three
    areas.

    1. Taxon validation. The program validates taxonomic names against the GBIF taxonomy
    backbone. By default, it uses the GBIF web API to validate names, but can also use a
    local copy of the backbone provided in a sqlite database: this will work offline and
    is much faster but requires some simple setup.

    2. Location names. The program also validate sampling location names against the SAFE gazeteer.
    By default, this is loaded automatically from the SAFE website so requires an internet
    connection, but a local copy can be provided for offline use.

    3. DOI checking. Optionally, the program will validate any DOIs provided as having used
    the database. This requires a web connection and cannot be performed offline.
    """

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument('fname', help="Path to the Excel file to be validated.")
    parser.add_argument('-c', '--check', default='sltwf',
                        help='Which of the summary, locations, taxa, worksheets and '
                             'finalisation should be checked.')
    parser.add_argument('-p', '--project_id', default=None, type=int, action='append',
                        help='If provided, check that the project ID within the file '
                             'matches this integer. Multiple values can be provided '
                             'to generate a set of valid IDs.')
    parser.add_argument('-l', '--locations_json', default=None,
                        help='Path to a locally stored json file of valid location names')
    parser.add_argument('-g', '--gbif_database', default=None,
                        help=('The path to a local sqlite database containing the GBIF '
                              'taxonomy backbone.'))
    parser.add_argument('--validate_doi', action="store_true", default=False,
                        help=('Check the validity of any publication DOIs, '
                              'provided by the user. Requires a web connection.'))

    args = parser.parse_args()

    check_file(fname=args.fname, verbose=True, locations_json=args.locations_json,
               gbif_database=args.gbif_database, validate_doi=args.validate_doi,
               check=args.check, project_id=args.project_id)


if __name__ == "__main__":
    main()
