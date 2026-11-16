"""
TABLEAU WORKBOOK FIELD DEPENDENCY ANALYZER
==========================================
Author: Octavia Isom
Date: 2025-01-13


WHAT THIS SCRIPT DOES
---------------------
It opens a Tableau workbook (.twb or .twbx) and works out which data
fields are actually being used, and where. It then exports 4
CSV files:


  1. field_inventory.csv    - Every field in the workbook, plus whether
                              it is used anywhere and in which sheets.
                              Includes filter columns (Used as Filter,
                              Filter Count, Filter Worksheets, etc).
  2. dependencies.csv       - One row per field-per-worksheet, showing
                              exactly where each field is used.
                              Dependency is "Direct", "Filter" or
                              "Via calculation".
  3. calculated_fields.csv  - Every calculated field, its formula, and
                              the other fields that it depends on.
  4. filters.csv            - One row per filter field per worksheet:
                              the dashboards it affects, filter type,
                              whether it is a context filter, whether
                              it is shown as a dashboard filter card,
                              and its saved (default) value.


HOW IT WORKS
------------------------------
A Tableau workbook is secretly just an XML text file (a .twbx is a ZIP
archive containing that XML). XML is a tree of tags, a bit like HTML.
This script reads that tree, finds every field definition, then scans
the worksheet sections looking for field references written in square
brackets like [Sales]. Anything it finds is records as "used".




HOW TO RUN IT
-------------
  1. Put your workbook file in the same folder as this script.
  2. Change WORKBOOK_NAME near the bottom of this file to match the
     exact file name of your workbook. Double check the file extension.
  3. Run it!




"""


# ---------------------------------------------------------------------
# IMPORTS
# ---------------------------------------------------------------------
import pandas as pd
from pathlib import Path # handle file and folder locations
from collections import defaultdict #dictionary that invents a default value when the key does not exist yet
from xml.etree import ElementTree as ET #XML reader
import zipfile #for looking inside .twbx files, which are really ZIP files
import re #regular expressions library




# ---------------------------------------------------------------------
# TEXT PATTERNS - compiled once up front so it doesn't have to be done
# multiple times inside a loop
# ---------------------------------------------------------------------


#Capture the a string between square brackets
# Ex. [Sales] -> Sales
FIELD_REF = re.compile(r'\[([^\]]+)\]')


# Captures the data source name and the field name for fully-qualified references.
# Ex. [Orders].[Sales] -> Orders.Sales
QUALIFIED_REF = re.compile(r'\[([^\]]+)\]\.\[([^\]]+)\]')


# Captures quoted text inside a Tableau formula. Ex. "North" -> North
# Otherwise a formula like IF [Region] = "[Not a field]" would
# fool us into thinking "[Not a field]" is a real field
STRING_LITERAL = re.compile(r'''"(?:[^"]|"")*"|'(?:[^']|'')*' '''.strip())


# Tableau stores some of its own UI hints in a custom XML namespace. ElementTree
# shows attributes from that namespace with this prefix in front of the name,
# e.g. {http://...}ui-action-filter
UI_NS = "{http://www.tableausoftware.com/xml/user}"


# Separator used between filter members inside one cell of the output.
# Members can contain commas, so a comma would be ambiguous.
MEMBER_SEP = " | "




# ---------------------------------------------------------------------
# FILTER HELPER FUNCTIONS
#
# These do not need any of the workbook lookup tables, so they live
# outside analyze_tableau().
# ---------------------------------------------------------------------
def clean_member(raw):
    """
    Tidy one filter member for display.


    Param:  raw: the member text as stored in the XML, e.g. '"ANZ"' or '%null%'
    Returns: a readable string, e.g. 'ANZ' or 'NULL'
    """
    if raw is None:
        return ""
    if raw == "%null%":
        return "NULL"
    # String members are stored wrapped in quotes. Strip only the
    # outer pair. Numbers, dates (#2019-04-01 00:00:00#) and anything
    # else are kept exactly as Tableau stored them.
    if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1]
    return raw




def evaluate_groupfilter(node):
    """
    Work out which members a categorical filter selects.


    Tableau stores a categorical filter as a tree of <groupfilter> tags:
        member         -> one specific value
        level-members  -> every value
        empty-level    -> the empty / blank value
        union          -> combine the children
        except         -> first child with the remaining children removed


    Param:  node: a <groupfilter> XML element
    Returns: (mode, members, note)
        mode    : "all"      every value is selected
                  "include"  only the listed members are selected
                  "exclude"  everything EXCEPT the listed members
                  "unknown"  structure not supported; do not guess
        members : list of member strings (empty for "all" / "unknown")
        note    : explanation, filled in only when mode is "unknown"
    """
    func = node.get("function")
    kids = node.findall("groupfilter")


    if func == "member":
        return "include", [clean_member(node.get("member"))], ""


    if func == "level-members":
        return "all", [], ""


    if func == "empty-level":
        return "include", ["(empty)"], ""


    if func == "union":
        members = []
        for kid in kids:
            mode, kid_members, note = evaluate_groupfilter(kid)
            if mode == "unknown":
                return "unknown", [], note
            if mode == "all":
                return "all", [], ""
            if mode == "exclude":
                return "unknown", [], "union containing an exclusion"
            members += [m for m in kid_members if m not in members]
        return "include", members, ""


    if func == "except" and len(kids) >= 2:
        base_mode, base_members, note = evaluate_groupfilter(kids[0])
        if base_mode == "unknown":
            return "unknown", [], note


        removed = []
        for kid in kids[1:]:
            mode, kid_members, note = evaluate_groupfilter(kid)
            if mode != "include":
                return "unknown", [], note or "except removing a non-member set"
            removed += kid_members


        if base_mode == "all":
            return "exclude", removed, ""
        if base_mode == "include":
            return "include", [m for m in base_members if m not in removed], ""
        return "unknown", [], "except applied to an exclusion"


    return "unknown", [], f"unsupported groupfilter function: {func}"




def describe_filter(filt):
    """
    Turn one worksheet <filter> element into its saved (default) state.


    The workbook only stores what was selected when it was last saved.
    It does not store the live values in the database.


    Param:  filt: a <filter> XML element
    Returns: (default_text, determinable, note)
        default_text : human readable description of the saved state
        determinable : False when the state cannot be worked out from
                       the XML alone, so the reader knows to check it
        note         : why it could not be worked out (otherwise blank)
    """
    fclass = filt.get("class", "")


    # ---- Categorical: a list of selected / excluded members ----------
    if fclass == "categorical":
        # Filters created by a dashboard action hold no saved selection.
        # The values only exist at run time, when a user clicks.
        if any(g.get(UI_NS + "ui-action-filter") for g in filt.iter("groupfilter")):
            return (
                "Set at run time by dashboard action",
                False,
                "Action filter: values depend on what the user selects"
            )


        top = filt.find("groupfilter")
        if top is None:
            return "", False, "Categorical filter without a groupfilter"


        mode, members, note = evaluate_groupfilter(top)
        if mode == "all":
            return "All", True, ""
        if mode == "include":
            if not members:
                return "Nothing selected", True, ""
            return "Include: " + MEMBER_SEP.join(members), True, ""
        if mode == "exclude":
            return "All except: " + MEMBER_SEP.join(members), True, ""
        return "", False, note


    # ---- Quantitative: a numeric / date range ------------------------
    if fclass == "quantitative":
        included = filt.get("included-values", "")
        lo = filt.find("min")
        hi = filt.find("max")
        lo = lo.text if lo is not None else None
        hi = hi.text if hi is not None else None


        if included in ("in-range", "in-range-or-null"):
            if lo is None and hi is None:
                return "", False, f"{included} filter with no min or max saved in the workbook"
            suffix = " (or null)" if included == "in-range-or-null" else ""
            return (
                f"Range: {lo if lo is not None else '(no minimum)'}"
                f" to {hi if hi is not None else '(no maximum)'}{suffix}",
                True, ""
            )
        if included == "non-null":
            return "Non-null values only", True, ""
        if included == "all":
            return "All values", True, ""
        if included == "null":
            return "Null values only", True, ""
        return "", False, f"unrecognised included-values: {included}"


    # ---- Relative date: a window measured in periods -----------------
    if fclass == "relative-date":
        period = filt.get("period-type-v2") or filt.get("period-type") or "?"
        first = filt.get("first-period", "?")
        last = filt.get("last-period", "?")
        text = f"{period}: {first} to {last} (0 = current {period})"
        anchor = filt.get("anchor")
        if anchor:
            text += f", anchor {anchor}"
        text += (
            f"; include future={filt.get('include-future', '?')}"
            f"; include null={filt.get('include-null', '?')}"
        )
        return text, True, ""


    return "", False, f"unsupported filter class: {fclass}"




# ---------------------------------------------------------------------
# MAIN FUNCTION
# ---------------------------------------------------------------------
def analyze_tableau(workbook_path):
    """


    Param:  the path to a Tableau workbook file.
    Returns: four dataframes
             1. field_inventory
             2. dependencies
             3. calculated_fields
             4. worksheet_filters


    """
    # Convert workbook path into a proper Path object
    path = Path(workbook_path).expanduser()


    # Eary fail when file is not there
    if not path.is_file():
        raise FileNotFoundError(f"Workbook not found: {path}")


    # -----------------------------------------------------------------
    # READ THE TABLEAU WORKBOOK
    #
    # Conditions:
    #   When the tableau file is .twb, it is already XML so parse it.
    #   if its a .twbx, which is a ZIP archive containing a .twb file,
    #   unzip it then parse the .twb file. Otherwise, raise an error
    # -----------------------------------------------------------------


   
    if path.suffix.lower() == ".twb":
        root = ET.parse(path).getroot()
    elif path.suffix.lower() == ".twbx":
        with zipfile.ZipFile(path) as z:
            twbs = [
                n for n in z.namelist()
                if n.lower().endswith(".twb")
            ]
            if not twbs:
                raise ValueError("No .twb found inside the .twbx")
            root = ET.fromstring(z.read(twbs[0]))
    else:
        raise ValueError("Use a .twb or .twbx workbook")




    # -----------------------------------------------------------------
    # LOOKUP TABLES WE FILL IN AS WE GO
    #
    # IMPORTANT: field names are not unique across a workbook -
    # two different data sources can both have a field called "Sales".
    # So almost everywhere below, a field is identified by a tuple:
    #     (data_source_id, field_id)
    # -----------------------------------------------------------------


    # Internal data source ID -> the user friendly name shown in the Tableau UI.
    names = {}


    #user friendly field name
    fields = {}


    # Maps Tableau's "column instance" aliases back to the real field.
    # e.g. [sum:Sales:qk] really means [Sales].
    instances = {}


    # the formula text, for calculated fields only. Plain fields pulled
    # straight from the database do not appear here.
    formulas = {}


    # Worksheet name -> the set of data source IDs that sheet uses.
    # A "set" is an unordered bag of unique values; adding a duplicate
    # does nothing, which is exactly what we want here.
    sheet_sources = {}


    # -----------------------------------------------------------------
    # 2. COLLECT FIELDS AND CALCULATED FIELDS
    # -----------------------------------------------------------------
    def add_columns(parent, ds):
        """
        Record every field defined under one XML section.


             Param:
                parent: is the XML chunk to look inside
                ds: is the data source ID those fields belong to
            Returns: None
        We call this more than once because Tableau declares fields in several places.
        """
       
        for col in parent.findall("./column"):
            fid = col.get("name")
            # Skip anything without an ID, its unusable
            if not fid:
                continue


            # The pair that uniquely identifies this field
            key = (ds, fid)


            fields[key] = (
                col.get("caption")      # 1. the UI name, if present
                or fields.get(key)      # 2. a name we found earlier
                or fid.strip("[]")      # 3. fall back to the raw ID
            )


            # Does this field have a formula nested inside it?
            calc = col.find("./calculation")


            # Three conditions must all hold for it to count as a real
            # calculated field we care about:
            if (
                calc is not None                      # a formula exists
                and calc.get("formula") is not None   # it has content
                and ds != "Parameters"                # not a parameter
            ):
                formulas[key] = calc.get("formula")


        # Now handle the aggregation aliases described earlier, so that
        # [sum:Sales:qk] can later be traced back to [Sales].
        for inst in parent.findall("./column-instance"):
            # Only record it if both halves are present.
            if inst.get("name") and inst.get("column"):
                instances[(ds, inst.get("name"))] = (
                    inst.get("column")
                )


    # go into <datasources>, then grab each <datasource> inside it
    for ds in root.findall("./datasources/datasource"):
        dsid = ds.get("name")


        if dsid:
            # Remember the user friendly name, falling back to the raw ID if needed
            names[dsid] = ds.get("caption") or dsid
            # And catalogue all the fields it contains
            add_columns(ds, dsid)




    # -----------------------------------------------------------------
    # IDENTIFY THE DATA SOURCES ASSOCIATED WITH EACH SHEET
    #
    # We need this because a bare reference like [Sales] is ambiguous.
    # Knowing which data sources a sheet uses lets us narrow down which
    # "Sales" is meant.
    # -----------------------------------------------------------------
    sheets = root.findall("./worksheets/worksheet")


    for sheet in sheets:
        # Second argument to .get() is the default if it is missing.
        sname = sheet.get("name", "")
        sources = set()


        #  means "search at any depth below ".//", not just direct
        # the children. Tableau nests these quite deeply.
        for ds in sheet.findall(
            ".//view/datasources/datasource"
        ):
            dsid = ds.get("name")


            if dsid:
                sources.add(dsid)
                # setdefault only writes if the key is NOT already
                # there, so we never overwrite a better name we
                # already captured in previous step
                names.setdefault(
                    dsid, ds.get("caption") or dsid
                )


        # Sheets also list their dependencies separately, and these
        # sections sometimes define fields we have not seen yet
        for deps in sheet.findall(
            ".//datasource-dependencies"
        ):
            dsid = deps.get("datasource")


            if dsid:
                sources.add(dsid)
                names.setdefault(dsid, dsid)
                # Pick up any extra field definitions declared here
                add_columns(deps, dsid)


        sheet_sources[sname] = sources


    # -----------------------------------------------------------------
    # RESOLVE TABLEAU'S INTERNAL FIELD REFERENCES
    #
    # This is the fiddliest part. Tableau refers to the same field in
    # several different ways, and we need them all to point at one
    # single (data_source, field_id) pair.
    # -----------------------------------------------------------------
    def resolve(ds, raw):
        """
        Turn one raw reference into a definite field, or return None if we cannot be sure.


        Param:
            ds: the data source to look in
            raw: the text found between square brackets, e.g. "Sales"
        Returns:
            The (ds, field_id) pair, or None if we cannot be sure
        """
        # Put the brackets back on to match Tableau's stored format
        fid = f"[{raw}]"


        # If this is an aggregation alias swap in the real field ID.
        # .get(key, default) returns the default when the key is
        # absent, so an unrecognised alias is simply left unchanged
        fid = instances.get((ds, fid), fid)


        key = (ds, fid)


        # Best case: we already know this exact field. Done.
        if key in fields:
            return key


        # Otherwise the reference might be a user friendly display name
        # rather than an internal ID. Search for fields in this data
        # source whose display name matches.
        matches = [
            k for k, caption in fields.items()
            if k[0] == ds and caption == raw
        ]


        # Only accept the answer if there is EXACTLY one match. If two
        # fields share a display name we cannot tell which was meant,
        # so we deliberately return None rather than guessing
        return matches[0] if len(matches) == 1 else None


    def refs(value, sources, is_formula=False):
        """
        Find every field referenced in a piece of text


        Param:
            value: the text to scan (an XML attribute or a formula)
            sources: which data sources are in scope for this text
            is_formula: True when scanning a calculation, which means
                        quoted text needs stripping out first
        Returns:
            A set of (data_source, field_id) pairs
        """
        # Early exit for anything that cannot contain a reference
        # Checking for "[" first avoids running expensive regexes on
        # the thousands of irrelevant attributes in the file.
        if not isinstance(value, str) or "[" not in value:
            return set()


        # Delete quoted text so words inside strings are not mistaken
        # for field names
        if is_formula:
            value = STRING_LITERAL.sub("", value)


        found = set()


        # --- Pass 1: qualified references, e.g. [Orders].[Sales] ---
        # These are unambiguous because the data source is named.
        for ds_raw, field_raw in QUALIFIED_REF.findall(value):
            if ds_raw in names:
                # Its already an internal ID
                dsids = [ds_raw]
            else:
                # Its a user friendly name, so find the matching ID(s)
                dsids = [
                    d for d, display in names.items()
                    if display == ds_raw
                ]


            for ds in dsids:
                key = resolve(ds, field_raw)
                if key:
                    found.add(key)


        # --- Pass 2: bare references, e.g. [Sales] ---
        # Remove the qualified ones we just handled, so their field
        # halves are not counted a second time in this pass.
        value = QUALIFIED_REF.sub("", value)


        for raw in FIELD_REF.findall(value):
            # Try to resolve this name against every data source the
            # sheet uses, and collect whatever we find
            # The ":=" is the "walrus operator", it assigns to "key"
            # and tests the value in one step.
            candidates = {
                key
                for ds in sources
                if (key := resolve(ds, raw)) is not None
            }


            # Same safety rule as in resolve(), only accept it when
            # there is a single unambiguous answer
            if len(candidates) == 1:
                found.update(candidates)


        return found


    # -----------------------------------------------------------------
    # BUILD CALCULATED FIELD DEPENDENCY RELATIONSHIPS
    #
    # For each calculation, work out its direct inputs
    # -----------------------------------------------------------------
    # This builds a whole dictionary in one expression. for every calculation,
    # scan its formula and store the set of fields it mentions.
    calc_inputs = {
        key: (
            refs(
                formula,
                # Fields can come from the calculation's own data
                # source, or from Parameters, which are global.
                {key[0], "Parameters"},
                is_formula=True
            )
            # Subtracting removes the calculation itself, which stops a
            # self-referencing formula creating an infinite loop.
            - {key}
        )
        for key, formula in formulas.items()
    }


    def upstream(key, visited=None):
        """
        Find all fields feeding a calculation, whether directly or indirectly


        Param:
            key: the (data_source, field_id) pair of the calculation we are interested in
            visited: a set of (data_source, field_id) pairs that have already been visited
        Returns:
            A set of (data_source, field_id) pairs
        """
        # "visited" tracks where we have already been, which prevents
        # an endless loop if two calculations reference each other.
        # We avoid putting an empty set in the signature default
        # because, in Python, default values are shared between calls
        # and would accumulate junk. This is the standard workaround.
        visited = set() if visited is None else visited


        # if we have already seen this one, stop here to break the cycle
        if key in visited:
            return set()


        # Mark it as seen. Using "|" creates a new set rather than
        # modifying the caller's copy, keeping each branch independent
        visited = visited | {key}
        result = set()


        # For each direct input record it, then take the inputs of that input too
        for parent in calc_inputs.get(key, set()):
            result.add(parent)
            result.update(upstream(parent, visited))


        return result


    # -----------------------------------------------------------------
    # ASSOCIATE WORKSHEETS WITH DASHBOARDS
    #
    # So the report can tell you not just "this field is on Sheet 1"
    # but also "and Sheet 1 appears on the Executive dashboard".
    # -----------------------------------------------------------------
    sheet_names = {s.get("name") for s in sheets}


    # A normal dict would raise an error for an unknown key. This
    # defaultdict quietly hands back an empty set() instead, so we can
    # append without checking whether the key exists first.
    dashboards = defaultdict(set)


    for dash in root.findall("./dashboards/dashboard"):
        dash_name = dash.get("name", "")


        # Dashboards are laid out as a grid of "zones". A zone that is
        # named after a worksheet is that worksheet embedded on the
        # dashboard.
        for zone in dash.findall(".//zone"):
            if zone.get("name") in sheet_names:
                dashboards[zone.get("name")].add(dash_name)


    def label(key):
        """
        Get the user friendly name for a field, for display purposes


        Param:
            key: the (data_source, field_id) pair of the field we are interested in
        Returns:
            The user friendly name for the field, or the internal ID with its brackets stripped off
            if we never found a proper name
        """
        return fields.get(key, key[1].strip("[]"))


    # -----------------------------------------------------------------
    # EXCLUDE METADATA DECLARATIONS FROM DIRECT USAGE CHECKS
    #
    # This matters a lot for accuracy. Worksheet XML contains two very
    # different kinds of information:
    #   (a) the actual visualisation - what is really on the sheet;
    #   (b) metadata blocks listing every field the sheet COULD use.
    # If we scanned (b), every field would look used so we skip those blocks.
    # -----------------------------------------------------------------
    def live_nodes(node):
        """Walk the XML tree, skipping metadata-only branches and filters."""
        # Hitting a metadata block: return immediately, which skips
        # this node AND everything beneath it.
        # "slices" (the Filters shelf) is skipped because filters are
        # captured separately (see FILTER EXTRACTION below) and get their
        # own Dependency = "Filter" rows. Without this, a field used ONLY
        # as a filter would be mislabelled "Direct".
        if node.tag in (
            "datasource-dependencies",
            "datasources",
            "slices"
        ):
            return


        # A normal filter is skipped for the same reason (it is reported as
        # Dependency = "Filter" instead). The exception is the built-in
        # "Measure Names" filter, whose members are the measures actually
        # DISPLAYED on the sheet via Measure Values. Those are genuine
        # direct usage, so that one is still scanned. Built-in fields have
        # a name starting with ":" (e.g. [ds].[:Measure Names]).
        if node.tag == "filter":
            filter_field = node.get("column", "").rsplit("].[", 1)[-1]
            if not filter_field.startswith(":"):
                return


        # "yield" hands back one item at a time instead of building a
        # giant list in memory. This makes the function a "generator".
        yield node


        # Then do the same for every child, going all the way down.
        # "yield from" passes along everything the recursive call
        # produces.
        for child in node:
            yield from live_nodes(child)


    # -----------------------------------------------------------------
    # FILTER EXTRACTION
    #
    # Three kinds of filter are captured:
    #   1. Worksheet filters  - a <filter> element in the sheet's <view>
    #   2. Action filters     - a <filter> created by a dashboard action
    #                           (flagged by a "ui-action-filter" marker)
    #   3. Filters-shelf only - a field listed in the sheet's <slices> that
    #                           has NO saved <filter> element
    # -----------------------------------------------------------------


    # Dashboard action name -> details, so action filters can be traced
    # back to the action that created them.
    actions = {}
    for act in root.findall(".//actions/action"):
        cmd = act.find("command")
        if cmd is None or cmd.get("command") != "tsc:tsl-filter":
            continue
        params = {p.get("name"): p.get("value") for p in cmd.findall("param")}
        src = act.find("source")
        activation = act.find("activation")
        if src is None:
            source_text = ""
        elif src.get("worksheet"):
            source_text = (
                f"{src.get('dashboard')} / {src.get('worksheet')}"
                if src.get("dashboard") else src.get("worksheet")
            )
        else:
            source_text = src.get("dashboard") or f"datasource: {src.get('datasource')}"
        actions[act.get("name")] = {
            "caption": act.get("caption", ""),
            "source": source_text,
            "trigger": activation.get("type", "") if activation is not None else "",
            "target": params.get("target", ""),
            "excluded": params.get("exclude", ""),
        }


    # Dashboard name -> the filter-card columns shown on it. A "filter" zone
    # is a quick filter card that users can see and change.
    # We store the raw column string, which is also what a sheet's <filter>
    # uses, so the two can be compared directly.
    cards = defaultdict(set)
    for dash in root.findall("./dashboards/dashboard"):
        for zone in dash.findall(".//zone"):
            if zone.get("type-v2") == "filter" and zone.get("param"):
                cards[dash.get("name", "")].add(zone.get("param"))


    filter_rows = []
    filter_fields_by_sheet = defaultdict(set)   # sheet -> {(ds, field_id)}


    for sheet in sheets:
        sname = sheet.get("name", "")
        source_ids = sheet_sources.get(sname, set())
        filter_columns_seen = set()


        def add_filter_row(raw_col, filt, source, keys, action_name=None, target_keys=frozenset()):
            """
            Append one output row per resolved field (or one unresolved row).


            target_keys are extra fields the filter is also applied to in
            other data sources (the filter's <target field="..."> children).
            """
            if filt is not None:
                default_text, determinable, note = describe_filter(filt)
                ftype = filt.get("class", "")
                is_context = filt.get("context") == "true"
            else:
                # Field is on the Filters shelf, but no filter state was saved.
                # The context flag is an attribute of the <filter> element, so
                # with no element there is nothing to read it from. It is
                # reported as "Unknown" rather than False, because False would
                # wrongly claim we checked and it is not a context filter.
                default_text, determinable = "", False
                note = "Field is on the Filters shelf (slices) but has no saved filter in the workbook"
                ftype, is_context = "", "Unknown"


            card_dashboards = sorted(
                d for d in dashboards[sname] if raw_col in cards[d]
            )
            info = actions.get(action_name, {}) if action_name else {}
            if action_name and not info:
                note = (note + "; " if note else "") + (
                    f"action {action_name} not found in the workbook's action list"
                )


            # A field name starting with ":" is one of Tableau's built-in
            # fields (e.g. Measure Names). It has no definition to resolve.
            tail = raw_col.rsplit("].[", 1)[-1]
            special = tail.startswith(":")


            target_only = set(target_keys) - set(keys)
            for key in sorted(set(keys) | target_only, key=str) or [None]:
                row_note = note
                if key in target_only:
                    row_note = "; ".join(n for n in (
                        note,
                        "Applied to this field via the filter's cross-data-source target"
                    ) if n)
                if key is None:
                    ds_id = raw_col.split("].[")[0].lstrip("[") if "].[" in raw_col else ""
                    if special:
                        extra = "Tableau built-in field (e.g. Measure Names), no field definition"
                    elif action_name:
                        extra = "Could not resolve the fields behind this action filter"
                    else:
                        extra = "Field could not be matched to a single known field"
                    row_note = "; ".join(n for n in (note, extra) if n)


                filter_rows.append({
                    "Worksheet": sname,
                    "Dashboards": ", ".join(sorted(dashboards[sname])),
                    "Data Source": names.get(key[0], key[0]) if key else names.get(ds_id, ds_id),
                    "Source ID": key[0] if key else ds_id,
                    "Filter Field": label(key) if key else raw_col,
                    "Field ID": key[1] if key else "",
                    "Filter Source": (
                        "Dashboard Action" if action_name
                        else "Worksheet Filter" if filt is not None
                        else "Filters shelf (no saved filter state)"
                    ),
                    "Filter Type": ftype,
                    "Is Context Filter": is_context,
                    "On Dashboard Filter Card": bool(card_dashboards),
                    "Filter Card Dashboards": ", ".join(card_dashboards),
                    "Default Value": default_text,
                    "Default Determinable": determinable,
                    "Default Note": row_note,
                    "Action Name": info.get("caption", "") if action_name else "",
                    "Action Source": info.get("source", ""),
                    "Action Trigger": info.get("trigger", ""),
                    "Action Target": info.get("target", ""),
                    "Action Excluded Sheets": info.get("excluded", ""),
                    "Field Resolved": key is not None,
                    "Raw Field Reference": raw_col,
                    "_key": key,
                })
                if key is not None:
                    filter_fields_by_sheet[sname].add(key)


        # --- (1) and (2): every saved <filter> element on this sheet ---
        for filt in sheet.findall(".//view/filter"):
            raw_col = filt.get("column", "")
            filter_columns_seen.add(raw_col)


            # Is this filter the product of a dashboard action?
            action_name = next(
                (
                    g.get(UI_NS + "ui-action-filter")
                    for g in filt.iter("groupfilter")
                    if g.get(UI_NS + "ui-action-filter")
                ),
                None
            )


            if action_name:
                # An action filter's column is a made-up "Action (...)"
                # label, so find the real fields from the levels inside.
                keys = set()
                for g in filt.iter("groupfilter"):
                    if g.get("level"):
                        keys |= refs(g.get("level"), source_ids)
            else:
                keys = refs(raw_col, source_ids)


            # A filter can also be applied to the matching field in another
            # data source, listed as <target field="..."> children.
            target_keys = set()
            for tgt in filt.findall("target"):
                target_keys |= refs(tgt.get("field", ""), source_ids)


            add_filter_row(raw_col, filt, "filter", keys, action_name, target_keys)


        # --- (3): on the Filters shelf, but no saved <filter> ---
        for col in sheet.findall(".//view/slices/column"):
            raw_col = (col.text or "").strip()
            if raw_col and raw_col not in filter_columns_seen:
                filter_columns_seen.add(raw_col)
                add_filter_row(raw_col, None, "slices", refs(raw_col, source_ids))


    filter_columns = [
        "Worksheet", "Dashboards", "Data Source", "Source ID", "Filter Field",
        "Field ID", "Filter Source", "Filter Type", "Is Context Filter",
        "On Dashboard Filter Card", "Filter Card Dashboards", "Default Value",
        "Default Determinable", "Default Note", "Action Name", "Action Source",
        "Action Trigger", "Action Target", "Action Excluded Sheets",
        "Field Resolved", "Raw Field Reference"
    ]
    worksheet_filters = pd.DataFrame(filter_rows, columns=filter_columns + ["_key"])
    worksheet_filters = (
        worksheet_filters.drop(columns="_key").drop_duplicates().reset_index(drop=True)
    )


    # -----------------------------------------------------------------
    # MAP FIELDS TO WORKSHEETS, INCLUDING INDIRECT DEPENDENCIES
    #
    # This produces the dependencies table: one row for every
    # field-and-worksheet combo
    # -----------------------------------------------------------------
    rows = []


    def add_dependency_rows(sname, used_fields, direct_label):
        """
        Append dependency rows for every field in used_fields, plus the
        fields feeding any that are calculations.


        Param:
            sname: worksheet name
            used_fields: set of (data_source, field_id) pairs used on the sheet
            direct_label: the "Dependency" text for the field itself
                          ("Direct" or "Filter")
        """
        for used in used_fields:
            # "{used} | upstream(used)" is the field itself PLUS
            # everything feeding into it. "|" means set union.
            for field in {used} | upstream(used):
                # Build one spreadsheet row as a dictionary, where the
                # keys become the column headings.
                rows.append({
                    "Data Source": names.get(
                        field[0], field[0]
                    ),
                    "Source ID": field[0],
                    "Field": label(field),
                    "Field ID": field[1],
                    "Worksheet": sname,
                    # Turn the set of dashboards into one tidy,
                    # alphabetically sorted, comma-separated string.
                    "Dashboards": ", ".join(
                        sorted(dashboards[sname])
                    ),
                    # Was this field used on the sheet itself, or is it
                    # only present because a calculation needs it?
                    "Dependency": (
                        direct_label
                        if field == used
                        else "Via calculation"
                    ),
                    # For indirect usage, name the calculation that
                    # pulls this field in. This is the column that
                    # tells you WHY a field cannot be deleted.
                    "Used Through": (
                        ""
                        if field == used
                        else label(used)
                    )
                })


    for sheet in sheets:
        sname = sheet.get("name", "")
        direct = set()                      # fields visibly on sheet
        source_ids = sheet_sources[sname]   # data sources in scope


        # Visit every meaningful XML node on this sheet.
        for node in live_nodes(sheet):
            # Field references can hide in two places: in attribute
            # values, and in the text between tags. Gather both.
            values = list(node.attrib.values())


            if node.text:
                values.append(node.text)


            # Scan each one and add whatever fields we recognise.
            for value in values:
                direct.update(refs(value, source_ids))


        # Now expand from direct usage to the full dependency chain.
        add_dependency_rows(sname, direct, "Direct")


        # Fields used as filters on this sheet get their own rows.
        add_dependency_rows(sname, filter_fields_by_sheet[sname], "Filter")


    # Fixing the column order explicitly keeps the CSV layout stable,
    # and guarantees valid empty columns if a workbook yields no rows.
    detail_cols = [
        "Data Source",
        "Source ID",
        "Field",
        "Field ID",
        "Worksheet",
        "Dashboards",
        "Dependency",
        "Used Through"
    ]


    # Hand the list of row-dictionaries to pandas to make a real table.
    # drop_duplicates() removes identical rows, which happen when the
    # same field is referenced several times on one sheet.
    dependencies = pd.DataFrame(
        rows, columns=detail_cols
    ).drop_duplicates()


    # -----------------------------------------------------------------
    # FIND ALL CALCULATIONS DEPENDING ON EACH FIELD
    #
    # Step 5 determined "what does this calculation need?". This flips
    # the question round: "what would break if I deleted this field?"
    # -----------------------------------------------------------------
    downstream_calcs = defaultdict(set)


    for calc in formulas:
        for field in upstream(calc):
            downstream_calcs[field].add(calc)


    # -----------------------------------------------------------------
    # CREATE THE FULL FIELD INVENTORY
    #
    # One row per field in the whole workbook, used or not. This is the
    # main report
    # -----------------------------------------------------------------
    inventory = []


    # Group the filter rows by field up front, so each inventory row can
    # look its own filter usage up directly.
    filters_by_field = defaultdict(list)
    for frow in filter_rows:
        if frow["_key"] is not None:
            filters_by_field[frow["_key"]].append(frow)


    for key in sorted(fields):
        field_filters = filters_by_field.get(key, [])
        filter_dashboards = {
            d.strip()
            for frow in field_filters
            for d in frow["Dashboards"].split(",")
            if d.strip()
        }
        # Pull out just the dependency rows for this one field.
        # This is pandas "boolean filtering": the bracketed conditions
        # produce a column of True/False, and we keep the True rows.
        # "&" means AND here (pandas requires & rather than "and").
        subset = dependencies[
            (dependencies["Source ID"] == key[0])
            & (dependencies["Field ID"] == key[1])
        ]


        # Each row holds dashboards as one comma-separated string, so
        # split them apart and collect the unique names. A set removes
        # duplicates automatically; .strip() tidies stray spaces.
        related_dashboards = {
            d.strip()
            for value in subset["Dashboards"]
            for d in value.split(",")
            if d.strip()
        }


        inventory.append({
            "Data Source": names.get(key[0], key[0]),
            "Source ID": key[0],
            "Field": label(key),
            "Field ID": key[1],
            # Classify the field. This chain reads as:
            #   a Parameter, else Calculated if it has a formula,
            #   else an ordinary database field.
            "Type": (
                "Parameter"
                if key[0] == "Parameters"
                else "Calculated"
                if key in formulas
                else "Base / other"
            ),
            # THE KEY COLUMN: False here means nothing references this
            # field, so it is a candidate for deletion.
            "Used in Worksheet": not subset.empty,
            # nunique() counts distinct values, so a field used twice
            # on one sheet still counts as a single worksheet.
            "Worksheet Count": subset["Worksheet"].nunique(),
            # Calculations that would break if this field were removed.
            "Dependent Calculations": ", ".join(
                sorted(
                    label(c)
                    for c in downstream_calcs[key]
                )
            ),
            "Worksheets": ", ".join(
                sorted(subset["Worksheet"].unique())
            ),
            "Dashboards": ", ".join(
                sorted(related_dashboards)
            ),
            # ---- Filter metadata ----
            # True when this field is filtered anywhere (worksheet filter,
            # dashboard action, or Filters shelf).
            "Used as Filter": bool(field_filters),
            # Number of distinct worksheets where this field is filtered.
            "Filter Count": len({f["Worksheet"] for f in field_filters}),
            "Filter Worksheets": ", ".join(
                sorted({f["Worksheet"] for f in field_filters})
            ),
            "Filter Dashboards": ", ".join(sorted(filter_dashboards)),
            # Distinct filter classes (categorical, quantitative, relative-date).
            # Filters-shelf-only entries have no class, so they are left out.
            "Filter Types": ", ".join(
                sorted({f["Filter Type"] for f in field_filters if f["Filter Type"]})
            ),
            # True    : at least one saved filter on this field is a context filter
            # False   : saved filters exist, and none is a context filter
            # Unknown : the field only appears on Filters shelves with no saved
            #           filter, so the workbook holds no context information
            # (blank) : the field is not used as a filter
            "Is Context Filter": (
                ""
                if not field_filters
                else True
                if any(f["Is Context Filter"] is True for f in field_filters)
                else False
                if any(f["Is Context Filter"] is False for f in field_filters)
                else "Unknown"
            ),
            "On Dashboard Filter Card": any(
                f["On Dashboard Filter Card"] for f in field_filters
            )
        })


    # -----------------------------------------------------------------
    # CREATE THE CALCULATED-FIELD INVENTORY
    #
    # A reference of every calculation, its formula, and what it is built from.
    # -----------------------------------------------------------------
    calcs = []


    # keeps the output in a predictable order, so
    # re-running the script produces comparable files.
    for key, formula in sorted(formulas.items()):
        calcs.append({
            "Data Source": names.get(key[0], key[0]),
            "Calculation": label(key),
            "Formula": formula,
            # Fields named directly in this formula.
            "Direct Inputs": ", ".join(
                sorted(
                    label(f)
                    for f in calc_inputs[key]
                )
            ),
            # Every field in the chain beneath it, at any depth.
            "All Upstream Fields": ", ".join(
                sorted(
                    label(f)
                    for f in upstream(key)
                )
            )
        })


    # -----------------------------------------------------------------
    # Hand all four finished tables back to the caller.
    # -----------------------------------------------------------------
    return (
        pd.DataFrame(inventory),
        dependencies,
        pd.DataFrame(calcs),
        worksheet_filters
    )




# =====================================================================
# RUN THE ANALYSIS
#⚠️⚠️⚠️ THIS IS THE ONLY SECTION YOU NEED TO EDIT. ⚠️⚠️⚠️


# =====================================================================


# STEP 1:
# Put your workbook in the same folder as this script, then set
# the name below to match it EXACTLY, including the .twb / .twbx
# extension. Keep the quotation marks around it.
WORKBOOK_NAME = "contact_center_dashboard.twbx"
dashboard_name = "contact_center"
site_name = "tableau"


# STEP 2:
# Work out which folder this script lives in, so the workbook is found
# no matter which directory you launch Python from. Without this, the
# script only works if your terminal happens to be in the right folder.
try:


    SCRIPT_DIR = Path(__file__).resolve().parent
except NameError:
    # __file__ does not exist in Jupyter notebooks or the interactive
    # prompt, so fall back to the current working directory instead.
    SCRIPT_DIR = Path.cwd()


workbook_path = SCRIPT_DIR / WORKBOOK_NAME




# Run the analysis. It returns the four tables in this exact order
field_inventory, dependencies, calculated_fields, worksheet_filters = (
    analyze_tableau(workbook_path)
)


# Add a "Site" column at the front of every table, with the same value
# (site_name) on every row. The dictionary pairs each table with a name
# so the progress message can say which one it is working on.
for table_name, df in {
    "field_inventory": field_inventory,
    "dependencies": dependencies,
    "calculated_fields": calculated_fields,
    "worksheet_filters": worksheet_filters,
}.items():
    df.insert(0, "Site", site_name)
    print(f"Added Site column to {table_name}")


# Print a quick summary so you can sanity-check the results. If these
# numbers are all 0, the workbook probably did not load as expected.
print("Fields identified:", len(field_inventory))
print("Dependency relationships:", len(dependencies))
print("Calculated fields:", len(calculated_fields))
print("Worksheet filters:", len(worksheet_filters))


# -----------------------------------------------------------------
# SAVE THE RESULTS
#
# ⚠️⚠️⚠️ Any existing files with the same names are overwritten without
# warning, so rename or move them first if you need to keep an earlier run
# -----------------------------------------------------------------


# Report 1: every field, and whether anything uses it.
output_path = SCRIPT_DIR / f"{dashboard_name}_field_inventory.csv"
field_inventory.to_csv(output_path, index=False)
print("Wrote:", output_path)


# Report 2: the field-by-worksheet detail behind report 1.
output_path = SCRIPT_DIR / f"{dashboard_name}_dependencies.csv"
dependencies.to_csv(output_path, index=False)
print("Wrote:", output_path)


# Report 3: documentation for every calculated field.
output_path = SCRIPT_DIR / f"{dashboard_name}_calculated_fields.csv"
calculated_fields.to_csv(output_path, index=False)
print("Wrote:", output_path)


# Report 4: every filter, the worksheets / dashboards it affects, and its
# saved (default) state.
output_path = SCRIPT_DIR / f"{dashboard_name}_filters.csv"
worksheet_filters.to_csv(output_path, index=False)
print("Wrote:", output_path)




