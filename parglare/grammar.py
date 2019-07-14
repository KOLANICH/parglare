# -*- coding: utf-8 -*-
from __future__ import unicode_literals, print_function
from os import path
import sys
import re
import itertools
from copy import copy
from parglare.six import add_metaclass
from parglare.exceptions import GrammarError, ParserInitError
from parglare.recognizers import (StringRecognizer, RegExRecognizer,
                                  STOP_recognizer, EOF_recognizer,
                                  EMPTY_recognizer)
from parglare.common import Location, load_python_module
from parglare.termui import prints, s_emph, s_header, a_print, h_print
from parglare import termui

if sys.version < '3':
    text = unicode  # NOQA
else:
    text = str

# Associativity
ASSOC_NONE = 0
ASSOC_LEFT = 1
ASSOC_RIGHT = 2

# Priority
DEFAULT_PRIORITY = 10

# Multiplicity
MULT_ONE = '1'
MULT_OPTIONAL = '?'
MULT_ONE_OR_MORE = '+'
MULT_ZERO_OR_MORE = '*'

RESERVED_SYMBOL_NAMES = ['EOF', 'STOP', 'EMPTY']
SPECIAL_SYMBOL_NAMES = ['KEYWORD', 'LAYOUT']

MODIFIERS = ['left', 'right', 'shift', 'reduce', 'dynamic', 'ps', 'nops',
             'pse', 'nopse', 'greedy', 'nogreedy', 'finish', 'nofinish',
             'prefer']

THIS_LOCATION = Location(file_name=__file__)


def escape(instr):
    return instr.replace('\n', r'\n').replace('\t', r'\t')


class GrammarSymbol(object):
    """
    Represents an abstract grammar symbol.

    :param name: The name of this grammar symbol.
    :param location: The location where symbol is defined.
    :type location: :class:`Location`

    :param int prior: Priority used for disambiguation.
    :param bool dynamic: Should dynamic disambiguation be called to resolve
        conflict involving this symbol.
    :param dict meta: User meta-data.
    """
    def __init__(self, name, location=None, prior=DEFAULT_PRIORITY,
                 dynamic=False, meta=None):
        self.name = escape(name)
        self.location = location
        self.prior = prior
        self.dynamic = dynamic
        self.meta = meta
        self._hash = hash(self.name)

    def add_meta_data(self, name, value):
        if self.meta is None:
            self.meta = {}
        self.meta[name] = value

    def __getattr__(self, name):
        if self.meta is not None:
            attr = self.meta.get(name)
            if attr:
                return attr
        raise AttributeError

    def __unicode__(self):
        return str(self)

    def __str__(self):
        return self.name

    def __repr__(self):
        return "{}({})".format(type(self).__name__, str(self))

    def __hash__(self):
        return self._hash


class NonTerminal(GrammarSymbol):
    """
    Represents a non-termial symbol of the grammar.

    :param list productions: A list of alternative productions for this
        NonTerminal.
    :param assoc: Associativity for disambiguation
    :param bool ps: "prefer shift" strategy for this non-terminal.
    :param bool pse: "prefer shift over empty" strategy for this non-terminal.
    """
    def __init__(self, name, productions=None, assoc=ASSOC_NONE, ps=False,
                 pse=False, **kwargs):
        super(NonTerminal, self).__init__(name, **kwargs)
        self.productions = productions if productions is not None else []
        self.assoc = assoc
        self.ps = ps
        self.pse = pse


class Terminal(GrammarSymbol):
    """
    Represent a terminal symbol of the grammar.

    :param action: A name of common/user action given in the grammar.
    :param bool finish: Used for scanning optimization.  If this terminal is
        `finish` no other recognizers will be checked if this succeeds.  If not
        provided in the grammar implicit rules will be used during table
        construction.
    :param bool prefer: Prefer this recognizer in case of multiple recognizers
        match at the same place and implicit disambiguation doesn't resolve.
    :param bool keyword: `True` if this Terminal represents keyword.  `False`
        by default.
    :param callable recognizer: Called with input list of objects and position
        in the stream.  Should return a sublist of recognized objects.  The
        sublist should be rooted at the given position.
    """
    def __init__(self, name, recognizer=None, action=None, finish=None,
                 prefer=False, keyword=False, **kwargs):
        super(Terminal, self).__init__(name, **kwargs)
        self._recognizer = None
        self.recognizer = recognizer
        self.action = action
        self.finish = finish
        self.prefer = prefer
        self.keyword = keyword

    @property
    def recognizer(self):
        return self._recognizer

    @recognizer.setter
    def recognizer(self, value):
        self._recognizer = value


class Reference(object):
    """
    A name reference to a :class:`GrammarSymbol` used for cross-resolving
    during grammar construction.

    :param str name: The FQN name of the referred symbol.  This is the name of
        the original desuggared symbol without taking into account multiplicity
        and separator.
    :param Location location: Location object of this reference.
    :param str mult: Multiplicity of the RHS reference (used for regex
        operators ?, *, +).  See MULT_* constants above.  By default
        multiplicity is MULT_ONE.
    :param symbol separator: A reference to the separator symbol or the
        separator symbol itself if resolved.
    """
    def __init__(self, location, name):
        self.name = name
        self.location = location
        self.mult = MULT_ONE
        self.separator = None

    @property
    def mult_name(self):
        """
        Returns the name of the symbol that should be used if
        multiplicity/separator is used.
        """
        return make_multiplicity_name(
            self.name, self.mult,
            self.separator.name if self.separator else None)

    def clone(self):
        new_ref = Reference(self.location, self.name)
        new_ref.mult = self.mult
        new_ref.separator = self.separator
        return new_ref

    def __repr__(self):
        return self.name


AUGSYMBOL_NAME = "S'"

# This terminal is a special terminal used internally.
STOP = Terminal("STOP", STOP_recognizer)

# These two terminals are special terminals used in the grammars.
# EMPTY will match nothing and always succeed.
# EOF will match only at the end of the input string.
EMPTY = Terminal("EMPTY", EMPTY_recognizer, action='pass_none')
EOF = Terminal("EOF", EOF_recognizer, action='pass_none')


class Production(object):
    """
    Represent production from the grammar.

    :param GrammarSymbol symbol: A grammar symbol on the LHS of the production.
    :param ProductionRHS rhs:
    :param action: A name of common/user action given in the grammar.
    :param list assignments: A list of Assignment instances.
    :param int assoc: Associativity.  Used for ambiguity (shift/reduce)
        resolution.
    :param int prior: Priority.  Used for ambiguity (shift/reduce) resolution.
    :param bool dynamic: Is dynamic disambiguation used for this production.
    :param bool ps: `prefer_shifts` strategy for this production.  Only makes
        sense for GLR parser.  Default is `None` -- means use parser settings.
    :param bool pse: `prefer_shifts_over_empty` strategy for this production.
        Only makes sense for GLR parser.  Default is `None` -- means use parser
        settings.
    :param dict meta: User meta-data.
    :param int prod_id: Ordinal number of the production.
    :param int prod_symbol_id: A zero-based ordinal of alternative choice for
        this production grammar symbol.
    """

    def __init__(self, symbol, rhs, action=None, assignments=None,
                 assoc=ASSOC_NONE, prior=DEFAULT_PRIORITY, dynamic=False,
                 ps=None, pse=None, meta=None):
        self.symbol = symbol
        self.rhs = rhs if rhs else ProductionRHS()
        self.action = action
        self.assignments = assignments
        self.assoc = assoc
        self.prior = prior
        self.dynamic = dynamic
        self.ps = ps
        self.pse = pse
        self.meta = meta

    def __str__(self):
        if hasattr(self, 'prod_id'):
            return (s_header("%d:") + " %s " + s_emph("=") +
                    " %s") % (self.prod_id, self.symbol, self.rhs)
        else:
            return ("%s " + s_emph("=") + " %s") % (self.symbol, self.rhs)

    def __repr__(self):
        return 'Production({})'.format(str(self.symbol))

    def __getattr__(self, name):
        if self.meta is not None:
            attr = self.meta.get(name)
            if attr:
                return attr
        raise AttributeError


class ProductionRHS(list):
    def __getitem__(self, idx):
        try:
            while True:
                symbol = super(ProductionRHS, self).__getitem__(idx)
                if symbol is not EMPTY:
                    break
                idx += 1
            return symbol
        except IndexError:
            return None

    def __len__(self):
        return super(ProductionRHS, self).__len__() - self.count(EMPTY)

    def __str__(self):
        return " ".join([str(x) for x in self])

    def __repr__(self):
        return "<ProductionRHS([{}])>".format(
            ", ".join([str(x) for x in self]))


class Assignment(object):
    """
    General assignment (`=` or `?=`, a.k.a. `named matches`) in productions.
    Used also for references as LHS and assignment operator are optional.
    """
    def __init__(self, name, op, symbol, mult=MULT_ONE, rhs_idx=None):
        """
        :param str name: The name on the LHS of assignment.
        :param str op: Either a `=` or `?=`.
        :param symbol: A grammar symbol on the RHS or the symbol name
        :type symbol: `GrammarSymbol` or `str`
        :param str mult: Multiplicity of the RHS reference (used for regex
            operators ?, *, +).  See MULT_* constants above.  By default
            multiplicity is MULT_ONE.
        :param int rhs_idx: Index in the production RHS
        """
        self.name = name
        self.op = op
        self.symbol = symbol
        self.mult = mult
        self.rhs_idx = rhs_idx


class PGAttribute(object):
    """
    PGAttribute definition created by named matches.

    :param str name: The name of the attribute.
    :param str mult: Multiplicity of the attribute.  See MULT_* constants.
    :param str type_name: The type name of the attribute value(s).  It is also
        the name of the referring grammar rule.
    """
    def __init__(self, name, mult, type_name):
        self.name = name
        self.mult = mult
        self.type_name = type_name


class PGFile(object):
    """Objects of this class represent parglare grammar files.

    Grammar files can be imported using `import` keyword. Rules referenced from
    the imported grammar must be fully qualified by the grammar module name. By
    default the name of the target .pg file is the name of the module. `as`
    keyword can be used to override the default.

    Example:
    ```
    import `some/path/mygrammar.pg` as target
    ```

    Rules from file `mygrammar.pg` will be available under `target` namespace:

    ```
    MyRule: target.someRule+;
    ```

    Actions are by default loaded from the file named `<grammar>_actions.py`
    where `grammar` is basename of grammar file. Recognizers are loaded from
    `<grammar>_recognizers.py`. Actions and recognizers given this way are both
    optional. Furthermore, both actions and recognizers can be overriden by
    supplying actions and/or recognizers dict during grammar/parser
    instantiation.

    Attributes:

    productions (list of Production): Local productions defined in this file.
    terminals (dict of Terminal):
    classes (dict of ParglareClass): Dynamically created classes. Used by
        obj action.
    imports (dict): Mapping imported module/file local name to PGFile object.
    file_path (str): A full canonic path to the .pg file.
    grammar (PGFile): A root/grammar file.
    recognizers (dict of callables): A dict of Python callables used as a
        terminal recognizers.
    """
    def __init__(self, productions, terminals=None, classes=None, imports=None,
                 file_path=None, grammar=None, recognizers=None,
                 imported_with=None):
        self.productions = productions
        self.terminals = terminals
        self.classes = classes if classes else {}
        self.grammar = self if grammar is None else grammar

        self.file_path = path.realpath(file_path) if file_path else None
        self.imported_with = imported_with
        self.recognizers = recognizers
        self.actions = {}

        self.collect_and_unify_symbols()

        if self.file_path:
            self.grammar.imported_files[self.file_path] = self

        if imports:
            self.imports = {i.module_name: i for i in imports}
            for i in imports:
                i.grammar = self.grammar
                try:
                    i.load_pgfile()
                except IOError:
                    raise GrammarError(
                        location=Location(file_name=self.file_path),
                        message='Can\'t import file "{}".'.format(
                            i.file_path))
        else:
            self.imports = {}

        self.resolve_references()
        self.load_actions()
        self.load_recognizers()

    def collect_and_unify_symbols(self):
        """Collect non-terminals and terminals (both explicit and implicit/inline)
        defined in this file and make sure there is only one instance for each
        of them.

        """
        nonterminals_by_name = {}
        terminals_by_name = {}
        terminals_by_str_rec = {}

        # Check terminal uniqueness in both name and string recognition
        # and collect all terminals from explicit definitions.
        for terminal in self.terminals:
            if terminal.name in terminals_by_name:
                raise GrammarError(
                    location=terminal.location,
                    message='Multiple definitions of terminal rule "{}"'
                            .format(terminal.name))
            if isinstance(terminal.recognizer, StringRecognizer):
                rec = terminal.recognizer
                if rec.value in terminals_by_str_rec:
                    raise GrammarError(
                        location=terminal.location,
                        message='Terminals "{}" and "{}" match '
                        'the same string.'
                        .format(terminal.name,
                                terminals_by_str_rec[rec.value].name))
                terminals_by_str_rec[rec.value] = terminal
            terminals_by_name[terminal.name] = terminal

        self.terminals = terminals_by_name
        self.terminals_by_str_rec = terminals_by_str_rec

        # Collect non-terminals
        for production in self.productions:
            symbol = production.symbol
            symbol.imported_with = self.imported_with
            # Check that there is no terminal defined by the same name.
            if symbol.name in self.terminals:
                raise GrammarError(
                    location=symbol.location,
                    message='Rule "{}" already defined as terminal'
                    .format(symbol.name))
            # Unify all non-terminal objects
            if symbol.name in nonterminals_by_name:
                old_symbol = symbol
                new_symbol = nonterminals_by_name[symbol.name]
                production.symbol = new_symbol
            else:
                nonterminals_by_name[symbol.name] = symbol
                old_symbol = new_symbol = symbol
            new_symbol.productions.append(production)

            # Check grammar actions for rules/symbols.
            if new_symbol.action_name:
                if new_symbol.action_name != old_symbol.action_name:
                    raise GrammarError(
                        location=new_symbol.location,
                        message='Multiple different grammar actions '
                        'for rule "{}".'.format(new_symbol.name))

        self.nonterminals = nonterminals_by_name
        self.symbols_by_name = dict(nonterminals_by_name)
        self.symbols_by_name.update(self.terminals)

        # Add special terminals
        self.symbols_by_name['EMPTY'] = EMPTY
        self.symbols_by_name['EOF'] = EOF
        self.symbols_by_name['STOP'] = STOP

    def resolve_references(self):
        # Two pass resolving to enable referening symbols created during
        # resolving (e.g. multiplicity symbols).
        for pazz in [True, False]:
            for production in self.productions:
                for idx, ref in enumerate(production.rhs):
                    if isinstance(ref, Reference):
                        production.rhs[idx] = self.resolve_ref(ref, pazz)

    def register_symbol(self, symbol):
        self.symbols_by_name[symbol.name] = symbol

    def load_actions(self):
        """
        Loads actions from <grammar_name>_actions.py if the file exists.
        Actions must be collected with action decorator and the decorator must
        be called `action`.
        """
        actions_file = None
        if self.file_path:
            actions_file = path.join(
                path.dirname(self.file_path),
                "{}_actions.py".format(path.splitext(
                    path.basename(self.file_path))[0]))
            if path.exists(actions_file):
                mod_name = "{}actions".format(
                    self.imported_with.fqn
                    if self.imported_with is not None else "")
                actions_module = load_python_module(mod_name, actions_file)
                if not hasattr(actions_module, 'action'):
                    raise GrammarError(
                        Location(file_name=actions_file),
                        message='Actions file "{}" must have "action" '
                        'decorator defined.'.format(actions_file))
                self.actions = actions_module.action.all

    def load_recognizers(self):
        """Load recognizers from <grammar_name>_recognizers.py. Override
        with provided recognizers.

        """
        if self.file_path:
            recognizers_file = path.join(
                path.dirname(self.file_path),
                "{}_recognizers.py".format(path.splitext(
                    path.basename(self.file_path))[0]))

            if path.exists(recognizers_file):
                mod_name = "{}recognizers".format(
                    self.imported_with.fqn
                    if self.imported_with is not None else "")
                mod_recognizers = load_python_module(mod_name,
                                                     recognizers_file)
                recognizers = mod_recognizers.recognizer.all

                for recognizer_name, recognizer in recognizers.items():
                    symbol = self.resolve_symbol_by_name(
                        recognizer_name,
                        location=Location(file_name=recognizers_file))
                    if symbol is None:
                        raise GrammarError(
                            location=Location(file_name=recognizers_file),
                            message='Recognizer given for unknown '
                            'terminal "{}".'.format(recognizer_name)
                        )
                    if not isinstance(symbol, Terminal):
                        raise GrammarError(
                            location=Location(file_name=recognizers_file),
                            message='Recognizer given for non-terminal "{}".'
                            .format(recognizer_name))
                    symbol.recognizer = recognizer

    def resolve_ref(self, symbol_ref, first_pass=False):
        """Resolves given symbol reference.

        For local name search this file, for FQN use imports and delegate to
        imported file.

        On each resolved symbol productions in the root file are updated.

        If this is first pass do not fail on unexisting reference as there
        might be new symbols created during resolving (e.g. multiplicity
        symbols).

        """
        if isinstance(symbol_ref.separator, Reference):
            symbol_ref.separator = self.resolve_ref(symbol_ref.separator)

        symbol_name = symbol_ref.name
        symbol = self.resolve_symbol_by_name(symbol_name, symbol_ref.location)
        if not symbol:
            if first_pass:
                return symbol_ref
            else:
                raise GrammarError(
                    location=symbol_ref.location,
                    message='Unknown symbol "{}"'.format(symbol_name))

        mult = symbol_ref.mult
        if mult != MULT_ONE:
            # If multiplicity is used than we are referring to
            # suggared symbol

            separator = symbol_ref.separator \
                if symbol_ref.separator else None

            base_symbol = symbol
            symbol_name = symbol_ref.multiplicity_name
            symbol = self.resolve_symbol_by_name(symbol_name,
                                                 symbol_ref.location)
            if not symbol:
                # If there is no multiplicity version of the symbol we
                # will create one at this place
                symbol = self.make_multiplicity_symbol(
                    symbol_ref, base_symbol, separator, self.imported_with)

        return symbol

    def resolve_symbol_by_name(self, symbol_name, location=None):
        """
        Resolves symbol by fqn.
        """
        if '.' in symbol_name:
            import_module_name, name = symbol_name.split('.', 1)
            try:
                imported_pg_file = self.imports[import_module_name]
            except KeyError:
                raise GrammarError(
                    location=location,
                    message='Unexisting module "{}" in reference "{}"'
                    .format(import_module_name, symbol_name))
            return imported_pg_file.resolve_symbol_by_name(name, location)
        else:
            return self.symbols_by_name.get(symbol_name, None)

    def resolve_action_by_name(self, action_name):
        if action_name in self.actions:
            return self.actions[action_name]
        elif '.' in action_name:
            import_module_name, name = action_name.split('.', 1)
            if import_module_name in self.imports:
                imported_pg_file = self.imports[import_module_name]
                return imported_pg_file.resolve_action_by_name(name)

    def make_multiplicity_symbol(self, symbol_ref, base_symbol, separator,
                                 imported_with):
        """
        Creates new NonTerminal for symbol refs using multiplicity and
        separators.
        """
        mult = symbol_ref.multiplicity
        if mult in [MULT_ONE_OR_MORE, MULT_ZERO_OR_MORE]:
            symbol_name = make_multiplicity_name(
                symbol_ref.name, MULT_ONE_OR_MORE,
                separator.name if separator else None)
            symbol = self.symbols_by_name.get(symbol_name)
            if not symbol:
                # noqa See: http://www.igordejanovic.net/parglare/grammar_language/#one-or-more_1
                productions = []
                symbol = NonTerminal(symbol_name, productions,
                                     base_symbol.location,
                                     imported_with=imported_with)

                if separator:
                    productions.append(
                        Production(symbol,
                                   ProductionRHS([symbol,
                                                  separator,
                                                  base_symbol])))
                    symbol.action_name = 'collect_sep'
                else:
                    productions.append(
                        Production(symbol,
                                   ProductionRHS([symbol,
                                                  base_symbol])))
                    symbol.action_name = 'collect'

                productions.append(
                    Production(symbol, ProductionRHS([base_symbol])))

                self.register_symbol(symbol)

            if mult == MULT_ZERO_OR_MORE:
                productions = []
                symbol_one = symbol
                symbol_name = make_multiplicity_name(
                    symbol_ref.name, mult,
                    separator.name if separator else None)
                symbol = NonTerminal(symbol_name, productions,
                                     base_symbol.location,
                                     imported_with=imported_with)

                productions.extend([Production(symbol,
                                               ProductionRHS([symbol_one]),
                                               nops=True),
                                    Production(symbol,
                                               ProductionRHS([EMPTY]))])

                def action(_, nodes):
                    if nodes:
                        return nodes[0]
                    else:
                        return []

                symbol.grammar_action = action

                self.register_symbol(symbol)

        else:
            # MULT_OPTIONAL
            if separator:
                raise GrammarError(
                    location=symbol_ref.location,
                    message='Repetition modifier not allowed for '
                    'optional (?) for symbol "{}".'
                    .format(symbol_ref.name))
            productions = []
            symbol_name = make_multiplicity_name(symbol_ref.name, mult)
            symbol = NonTerminal(symbol_name, productions,
                                 base_symbol.location,
                                 imported_with=imported_with)
            productions.extend([Production(symbol,
                                           ProductionRHS([base_symbol])),
                                Production(symbol,
                                           ProductionRHS([EMPTY]))])

            symbol.action_name = 'optional'

            self.register_symbol(symbol)

        return symbol


class Grammar(object):
    """
    Grammar is a collection of production rules, nonterminals and terminals.
    First production is reserved for the augmented production (S' -> S).

    :param grammar_struct: Python structure that represents the grammar to be
        built
    :param classes: A dict of user specific classes
    :param file_path: A file path where grammar was loaded from
    :param start_symbol: start/root symbol of the grammar or its name.
    :type start_symbol: :class:`GrammarSymbol` or str

    :param nonterminals: A dict of :class:`NonTerminal` keyed by name
    :param terminals: A dict of :class:`Terminal` keyed by name
    """

    # Cache for PG grammar and parser table
    pg_parser_grammar = None
    pg_parser_table = None

    def __init__(self, grammar_struct, classes=None, file_path=None,
                 start_symbol=None, ignore_case=False, re_flags=re.MULTILINE,
                 debug=False, debug_parse=False, debug_colors=False):
        self.classes = classes if classes else {}
        self.file_path = file_path
        self.ignore_case = ignore_case
        self.re_flags = re_flags
        self.terminals = {}
        self.nonterminals = {}
        self.productions = []

        if start_symbol is None:
            if 'start' not in grammar_struct:
                raise GrammarError(
                    location=THIS_LOCATION,
                    message='No start symbol provided for the grammar.')
            else:
                start_symbol = grammar_struct['start']

        # Augmented prod. in LR automata calculation.
        if AUGSYMBOL_NAME not in grammar_struct['rules']:
            grammar_struct['rules'][AUGSYMBOL_NAME] = {
                'productions': [
                    {'production': [start_symbol, 'STOP']}
                ]
            }
            grammar_struct['start'] = AUGSYMBOL_NAME

        self.grammar_struct = grammar_struct
        self._init_from_struct()
        self.AUGSYMBOL = self.get_nonterminal(AUGSYMBOL_NAME)

    def _init_from_struct(self):
        """
        Initialize this grammar from a grammar given in a form of a Python
        structure.
        """
        self._extend_assignment_definitions()
        self._desugar_struct_multiplicities()
        self._create_terminals()
        self._create_productions()
        self._enumerate_productions()
        self._resolve()

    def _extend_assignment_definitions(self):
        """
        Extend assignment definition struct to include information about
        multiplicities and symbol names before multiplicities desugaring.
        """
        rules = self.grammar_struct['rules']
        for rule_name, rule_struct in list(rules.items()):
            for (prod_idx,
                 production_struct) in enumerate(rule_struct['productions']):
                rhs = production_struct['production']
                assignments_struct = production_struct.get('assignments', {})
                for assignment_struct in assignments_struct.values():
                    rhs_idx = assignment_struct['rhs_idx']
                    mult_struct = rhs[rhs_idx]
                    if 'mult' not in assignment_struct:
                        if type(mult_struct) is dict:
                            assignment_struct['mult'] = mult_struct['mult']
                        else:
                            assignment_struct['mult'] = MULT_ONE
                    if 'symbol' not in assignment_struct:
                        if type(mult_struct) is dict:
                            assignment_struct['symbol'] = \
                                mult_struct['symbol']
                        else:
                            assignment_struct['symbol'] = mult_struct

    def _desugar_struct_multiplicities(self):
        """
        Desugar grammar struct by creating additional multiplicities grammar
        symbols and reducing grammar struct to its canonical form.
        """
        rules = self.grammar_struct['rules']
        for rule_name, rule_struct in list(rules.items()):
            for (prod_idx,
                 production_struct) in enumerate(rule_struct['productions']):
                rhs = production_struct['production']
                for ref_idx, ref in enumerate(rhs):
                    if type(ref) is dict:
                        if 'symbol' not in ref:
                            raise GrammarError(
                                location=THIS_LOCATION,
                                message='"symbol" key must be given in '
                                'reference in rule "{}".'.format(rule_name))
                        self._degugar_multiplicity_ref(ref)
                        if len(ref) == 1:
                            # If we are reduced a reference only on symbol name
                            # replace with just a simple string
                            rhs[ref_idx] = ref['symbol']  # noqa

    def _degugar_multiplicity_ref(self, ref):
        """
        Desugar complex suggared reference containing multiplicity and
        separator definition to a canonical form of a reference.  Create
        necessary rules for repetitions (one or more, zero or more, optional).
        """
        symbol = ref['symbol']
        multiplicity = ref.get('mult', None)

        if not multiplicity:
            return

        separator = None
        modifiers = ref.get('modifiers', [])
        if modifiers:
            del ref['modifiers']
        for idx, modifier in enumerate(modifiers):
            if modifier not in MODIFIERS:
                if separator:
                    raise GrammarError(
                        location=Location(),
                        message='Multiple separators in reference "{}".'
                        .format(ref))
                separator = modifier
                del modifiers[idx]

        symbol_mult = self._make_multiplicity_name(symbol, multiplicity,
                                                   separator, modifiers)
        ref['symbol'] = symbol_mult
        del ref['mult']

        rules = self.grammar_struct['rules']
        if symbol_mult in rules:
            return

        if multiplicity in [MULT_ONE_OR_MORE, MULT_ZERO_OR_MORE]:
            # noqa See: http://www.igordejanovic.net/parglare/grammar_language/#one-or-more_1
            symbol_one = self._make_multiplicity_name(symbol, MULT_ONE_OR_MORE,
                                                      separator, modifiers)
            if symbol_one not in rules:
                productions = []
                if separator:
                    p = {'production': [symbol_one, separator, symbol]}
                else:
                    p = {'production': [symbol_one, symbol]}

                if modifiers:
                    p['modifiers'] = modifiers
                productions.append(p)
                productions.append(
                        {'production': [symbol]}
                )

                rules[symbol_one] = {
                    'action': 'collect_sep' if separator else 'collect',
                    'productions': productions
                }

            if multiplicity is MULT_ZERO_OR_MORE:
                rules[symbol_mult] = {
                    'productions': [
                        {'production': [symbol_one]},
                        {'production': ['EMPTY']}
                    ]
                }

        elif multiplicity in MULT_OPTIONAL:
            if separator:
                raise GrammarError(
                    location=THIS_LOCATION,
                    message='Repetition modifier not allowed for '
                    'optional (?) for symbol "{}".'.format(symbol))

            rules[symbol_mult] = {
                'action': 'optional',
                'productions': [
                    {'production': [symbol]},
                    {'production': ['EMPTY']}
                ]
            }

    def _create_terminals(self):
        """
        Create terminals of this grammar given the Python struct.
        """
        self.terminals.update([(s.name, s) for s in (EMPTY, EOF, STOP)])
        for terminal_name, terminal_struct \
                in self.grammar_struct.get('terminals', {}).items():
            check_name(None, terminal_name)
            if terminal_name in self.terminals:
                raise GrammarError(
                    location=THIS_LOCATION,
                    message=f'"{terminal_name}" terminal multiple definition')
            recognizer = None
            recognizer_str = terminal_struct.get('recognizer')
            if recognizer_str:
                if recognizer_str.startswith('/') \
                   and recognizer_str.endswith('/'):
                    recognizer = RegExRecognizer(recognizer_str[1:-1],
                                                 re_flags=self.re_flags,
                                                 ignore_case=self.ignore_case)
                else:
                    recognizer = StringRecognizer(recognizer_str,
                                                  ignore_case=self.ignore_case)
            term_modifiers = self._desugar_modifiers(
                terminal_struct.get('modifiers', []))
            terminal = Terminal(terminal_name,
                                recognizer=recognizer,
                                prior=term_modifiers.get('prior',
                                                         DEFAULT_PRIORITY),
                                finish=term_modifiers.get('finish', None),
                                prefer=term_modifiers.get('prefer', False),
                                dynamic=term_modifiers.get('dynamic', False),
                                keyword=term_modifiers.get('keyword', False),
                                action=terminal_struct.get('action',
                                                           terminal_name),
                                meta=terminal_struct.get('meta'))
            self.terminals[terminal_name] = terminal

    def _create_productions(self):
        """
        Create productions of this grammar given the Python struct.
        """
        for rule_name, rule_struct in self.grammar_struct['rules'].items():
            check_name(None, rule_name)
            if rule_name in self.nonterminals or rule_name in self.terminals:
                raise GrammarError(
                    location=THIS_LOCATION,
                    message=f'"{rule_name}" rule multiple definitions')
            rule_modifiers = self._desugar_modifiers(
                rule_struct.get('modifiers', []))
            nt = NonTerminal(
                rule_name,
                assoc=rule_modifiers.get('assoc', ASSOC_NONE),
                prior=rule_modifiers.get('prior', DEFAULT_PRIORITY),
                dynamic=rule_modifiers.get('dynamic', False),
                ps=rule_modifiers.get('ps', None),
                pse=rule_modifiers.get('pse', None),
                meta=rule_struct.get('meta')
            )
            self.nonterminals[rule_name] = nt
            productions = []
            rule_assignments = {}
            for production_struct in rule_struct['productions']:
                rhs = ProductionRHS(production_struct['production'])
                prod_modifiers = self._desugar_modifiers(
                    production_struct.get('modifiers', []))
                prod_assignments = {}
                assignments = production_struct.get('assignments', {})
                for assign_name, assignment_struct in assignments.items():
                    prod_assignments[assign_name] = \
                        Assignment(assign_name,
                                   op=assignment_struct['op'],
                                   symbol=assignment_struct['symbol'],
                                   mult=assignment_struct['mult'],
                                   rhs_idx=assignment_struct['rhs_idx'])
                productions.append(Production(
                    nt, rhs,
                    action=production_struct.get(
                        'action', rule_struct.get('action', rule_name)),
                    assignments=prod_assignments.values(),
                    assoc=prod_modifiers.get('assoc', nt.assoc),
                    prior=prod_modifiers.get('prior', nt.prior),
                    dynamic=prod_modifiers.get('dynamic', nt.dynamic),
                    ps=prod_modifiers.get('ps', nt.ps),
                    pse=prod_modifiers.get('pse', nt.pse),
                    meta=production_struct.get('meta')
                ))
                rule_assignments.update(prod_assignments)
            nt.productions = productions
            if rule_assignments and rule_name not in self.classes:
                self._create_class(rule_name, rule_assignments)
                nt.action = 'obj'
            # AUGMENTED symbol production must be first
            if rule_name == AUGSYMBOL_NAME:
                self.productions.insert(0, productions[0])
            else:
                self.productions.extend(productions)

    def _enumerate_productions(self):
        """
        Enumerates all productions (prod_id) and production per symbol
        (prod_symbol_id).
        """
        idx_per_symbol = {}
        for idx, prod in enumerate(self.productions):
            prod.prod_id = idx
            prod.prod_symbol_id = idx_per_symbol.get(prod.symbol, 0)
            idx_per_symbol[prod.symbol] = \
                idx_per_symbol.get(prod.symbol, 0) + 1

    def _create_class(self, rule_name, rule_assignments):
        """
        Create class for each rule using assignments.
        """
        attrs = {}
        for a in rule_assignments.values():
            attrs[a.name] = PGAttribute(a.name, a.mult, a.symbol)

        class ParglareMetaClass(type):

            def __repr__(cls):
                return '<parglare:{} class at {}>'.format(rule_name, id(cls))

        @add_metaclass(ParglareMetaClass)
        class ParglareClass(object):
            """
            Dynamically created class for each parglare rule that uses named
            matches.

            :param _pg_attrs: A dict of meta-attributes keyed by name.  Used by
                common rules.
            :param int _pg_position: A position in the input string where this
                class is defined.
            :param int _pg_position_end: A position in the input string where
                this class ends.
            """

            _pg_attrs = attrs

            def __init__(self, **attrs):
                for attr_name, attr_value in attrs.items():
                    setattr(self, attr_name, attr_value)

            def __repr__(self):
                if hasattr(self, 'name'):
                    return "<{}:{}>".format(rule_name, self.name)
                else:
                    return "<parglare:{} instance at {}>"\
                        .format(rule_name, hex(id(self)))

        ParglareClass.__name__ = rule_name

        self.classes[rule_name] = ParglareClass

    def _desugar_modifiers(self, modifiers):
        """
        Returns a dict of desugared modifiers values.
        """
        mod_dict = {}
        for m in modifiers:
            if type(m) is int:
                mod_dict['prior'] = m
            elif m in ['left', 'reduce']:
                mod_dict['assoc'] = ASSOC_LEFT
            elif m in ['right', 'shift']:
                mod_dict['assoc'] = ASSOC_RIGHT
            elif m in ['ps', 'nops', 'greedy', 'nogreedy']:
                mod_dict['ps'] = not m.startswith('no')
            elif m in ['pse', 'nopse']:
                mod_dict['pse'] = not m.startswith('no')
            elif m == 'dynamic':
                mod_dict['dynamic'] = True
            elif m in ['finish', 'nofinish']:
                mod_dict['finish'] = not m.startswith('no')
            elif m == 'prefer':
                mod_dict['prefer'] = True
            elif m == 'keyword':
                mod_dict['keyword'] = True
        return mod_dict

    def _fix_keyword_terminals(self):
        """
        If KEYWORD terminal with regex match is given fix all matching string
        recognizers to match on a word boundary.
        """
        keyword_term = self.get_terminal('KEYWORD')
        if keyword_term is None:
            return

        # KEYWORD rule must have a regex recognizer
        keyword_rec = keyword_term.recognizer
        if not isinstance(keyword_rec, RegExRecognizer):
            raise GrammarError(
                location=keyword_term.location,
                message='KEYWORD rule must have a regex recognizer defined.')

        # Change each string recognizer corresponding to the KEYWORD
        # regex by the regex recognizer that match on word boundaries.
        for term in self.terminals.values():
            if isinstance(term.recognizer, StringRecognizer):
                match = keyword_rec(term.recognizer.value, 0)
                if match == term.recognizer.value:
                    term.recognizer = RegExRecognizer(
                        r'\b{}\b'.format(match),
                        ignore_case=term.recognizer.ignore_case)
                    term.keyword = True

    def _resolve(self):
        """
        Resolve productions RHS references.
        """
        for production in self.productions:
            for refidx, ref in enumerate(production.rhs):
                if type(ref) is dict:
                    ref = ref['symbol']
                production.rhs[refidx] = self.get_check_symbol(
                    ref, production.symbol.name)

    def get_terminal(self, name):
        "Returns terminal with the given fully qualified name or name."
        return self.terminals.get(name)

    def get_nonterminal(self, name):
        "Returns non-terminal with the given fully qualified name or name."
        return self.nonterminals.get(name)

    def get_symbol(self, name):
        "Returns grammar symbol with the given name."
        s = self.get_terminal(name)
        if not s:
            s = self.get_nonterminal(name)
        return s

    def get_check_symbol(self, name, rule):
        "Returns symbol if exists or raise grammar error if not."
        symbol = self.get_symbol(name)
        if not symbol:
            raise GrammarError(
                location=THIS_LOCATION,
                message='Unknown symbol "{}" in rule "{}".'
                .format(name, rule))
        return symbol

    def __iter__(self):
        return (s for s in itertools.chain(self.nonterminals.values(),
                                           self.terminals.values())
                if s not in [self.AUGSYMBOL, STOP])

    def get_production_id(self, name):
        "Returns first production id for the given symbol name"
        for p in self.productions:
            if p.symbol.fqn == name:
                return p.prod_id

    @staticmethod
    def from_struct(grammar_struct, **kwargs):
        """
        Create grammar object from grammar given in a form of a Python data
        structure.
        """
        return Grammar(grammar_struct, **kwargs)

    @staticmethod
    def from_string(grammar_str, **kwargs):
        return Grammar.from_struct(Grammar.struct_from_string(grammar_str),
                                   **kwargs)

    # @staticmethod
    # def from_file(file_name, **kwargs):
    #     file_name = path.realpath(file_name)

    #     with codecs.open(file_name, 'w', encoding="utf-8") as f:
    #         content = f.read()
    #     return Grammar.from_struct(Grammar.struct_from_string(content),
    #                                **kwargs)

    @staticmethod
    def struct_from_string(grammar_str, **kwargs):
        """
        Parse grammar string and return the grammar represented as Python
        structure.
        """
        from parglare.lang import get_grammar_parser
        debug = kwargs.pop('debug', False)
        debug_colors = kwargs.pop('debug_colors', False)
        return get_grammar_parser(
            debug=debug, debug_colors=debug_colors).parse(grammar_str,
                                                          **kwargs)

    def print_debug(self):
        a_print("*** GRAMMAR ***", new_line=True)
        h_print("Terminals:")
        prints(" ".join([text(t) for t in self.terminals]))
        h_print("NonTerminals:")
        prints(" ".join([text(n) for n in self.nonterminals]))

        h_print("Productions:")
        for p in self.productions:
            prints(text(p))

    def _check_name(self, name, location=None):
        """
        Used in actions to check for reserved names usage.
        """

        if name in RESERVED_SYMBOL_NAMES:
            raise GrammarError(
                location=location,
                message='Rule name "{}" is reserved.'.format(name))
        if '.' in name:
            raise GrammarError(
                location=location,
                message='Using dot in names is not allowed.'.format(name))

    def _make_multiplicity_name(self, symbol_name, multiplicity=None,
                                separator_name=None, modifiers=None):
        if multiplicity is None or multiplicity == MULT_ONE:
            return symbol_name
        name_by_mult = {
            MULT_ZERO_OR_MORE: "0",
            MULT_ONE_OR_MORE: "1",
            MULT_OPTIONAL: "opt"
        }
        name = "{}_{}{}".format(
            symbol_name, name_by_mult[multiplicity],
            "_{}".format(separator_name) if separator_name else "")
        if modifiers:
            modifiers = list(sorted(map(lambda x: str(x), modifiers)))
            mod_name_sufix = "_".join(modifiers)
        name += "_{}".format(mod_name_sufix) if modifiers else ""
        return name


class _Grammar(PGFile):
    """
    Grammar is a collection of production rules, nonterminals and terminals.
    First production is reserved for the augmented production (S' -> S).

    :param start_symbol: start/root symbol of the grammar or its name.
    :type start_symbol: GrammarSymbol or str

    :param nonterminals: terminals(set of Terminal): imported_files(dict):
        Global registry of all imported files.
    :type nonterminals: set of NonTerminal
    """

    def __init__(self, productions=None, terminals=None,
                 classes=None, imports=None, file_path=None, recognizers=None,
                 start_symbol=None, _no_check_recognizers=False,
                 re_flags=re.MULTILINE, ignore_case=False, debug=False,
                 debug_parse=False, debug_colors=False):
        """
        Grammar constructor is not meant to be called directly by the user.
        See `from_str` and `from_file` static methods instead.

        Arguments:
        see Grammar attributes.
        _no_check_recognizers (bool, internal): Used by pglr tool to circumvent
             errors for empty recognizers that will be provided in user code.
        """

        self.imported_files = {}

        super(Grammar, self).__init__(productions=productions,
                                      terminals=terminals,
                                      classes=classes,
                                      imports=imports,
                                      file_path=file_path,
                                      grammar=self,
                                      recognizers=recognizers)

        self._no_check_recognizers = _no_check_recognizers

        # Determine start symbol. If name is provided search for it. If name is
        # not given use the first production LHS symbol as the start symbol.
        if start_symbol:
            if isinstance(start_symbol, str):
                for p in self.productions:
                    if p.symbol.name == start_symbol:
                        self.start_symbol = p.symbol
            else:
                self.start_symbol = start_symbol
        else:
            # By default, first production symbol is the start symbol.
            self.start_symbol = self.productions[0].symbol

        self._init_grammar()

    def _init_grammar(self):
        """
        Extracts all grammar symbol (nonterminal and terminal) from the
        grammar, resolves and check references in productions, unify all
        grammar symbol objects and enumerate productions.
        """
        # Reserve 0 production. It is used for augmented prod. in LR
        # automata calculation.
        self.productions.insert(
            0,
            Production(AUGSYMBOL, ProductionRHS([self.start_symbol, STOP])))

        self._add_all_symbols_productions()
        self._enumerate_productions()
        self._fix_keyword_terminals()
        self._resolve_actions()

        # Connect recognizers, override grammar provided
        if not self._no_check_recognizers:
            self._connect_override_recognizers()

    def _add_all_symbols_productions(self):

        self.nonterminals = {}
        for prod in self.productions:
            self.nonterminals[prod.symbol.fqn] = prod.symbol
        self.terminals.update([(s.name, s) for s in (EMPTY, EOF, STOP)])

        def add_productions(productions):
            for production in productions:
                symbol = production.symbol
                if symbol.fqn not in self.nonterminals:
                    self.nonterminals[symbol.fqn] = symbol
                for idx, rhs_elem in enumerate(production.rhs):
                    if isinstance(rhs_elem, Terminal):
                        if rhs_elem.fqn not in self.terminals:
                            self.terminals[rhs_elem.fqn] = rhs_elem
                        else:
                            # Unify terminals
                            production.rhs[idx] = self.terminals[rhs_elem.fqn]
                    elif isinstance(rhs_elem, NonTerminal):
                        if rhs_elem.fqn not in self.nonterminals:
                            self.productions.extend(rhs_elem.productions)
                            add_productions(rhs_elem.productions)
                    else:
                        # This should never happen
                        assert False, "Invalid RHS element type '{}'."\
                            .format(type(rhs_elem))
        add_productions(list(self.productions))

    def _enumerate_productions(self):
        """
        Enumerates all productions (prod_id) and production per symbol
        (prod_symbol_id).
        """
        idx_per_symbol = {}
        for idx, prod in enumerate(self.productions):
            prod.prod_id = idx
            prod.prod_symbol_id = idx_per_symbol.get(prod.symbol, 0)
            idx_per_symbol[prod.symbol] = \
                idx_per_symbol.get(prod.symbol, 0) + 1

    def _fix_keyword_terminals(self):
        """
        If KEYWORD terminal with regex match is given fix all matching string
        recognizers to match on a word boundary.
        """
        keyword_term = self.get_terminal('KEYWORD')
        if keyword_term is None:
            return

        # KEYWORD rule must have a regex recognizer
        keyword_rec = keyword_term.recognizer
        if not isinstance(keyword_rec, RegExRecognizer):
            raise GrammarError(
                location=keyword_term.location,
                message='KEYWORD rule must have a regex recognizer defined.')

        # Change each string recognizer corresponding to the KEYWORD
        # regex by the regex recognizer that match on word boundaries.
        for term in self.terminals.values():
            if isinstance(term.recognizer, StringRecognizer):
                match = keyword_rec(term.recognizer.value, 0)
                if match == term.recognizer.value:
                    term.recognizer = RegExRecognizer(
                        r'\b{}\b'.format(match),
                        ignore_case=term.recognizer.ignore_case)
                    term.keyword = True

    def _resolve_actions(self, action_overrides=None,
                         fail_on_no_resolve=False):
        """
        Checks and resolves semantic actions given in the grammar and
        additional `*_actions.py` module.

        Args:
            action_overrides(dict): Dict of actions that take precendence. Used
                for actions supplied during parser construction.
        """
        import parglare.actions as actmodule

        for symbol in self:

            # Resolve trying from most specific to least specific
            action = None

            # 1. Resolve by fully qualified symbol name
            if '.' in symbol.fqn:
                if action_overrides:
                    action = action_overrides.get(symbol.fqn, None)

                if action is None:
                    action = self.resolve_action_by_name(symbol.fqn)

            # 2. Fully qualified action name
            if action is None and symbol.action_fqn is not None \
               and '.' in symbol.action_fqn:
                if action_overrides:
                    action = action_overrides.get(symbol.action_fqn, None)

                if action is None:
                    action = self.resolve_action_by_name(symbol.action_fqn)

            # 3. Symbol name
            if action is None:
                if action_overrides:
                    action = action_overrides.get(symbol.name, None)

                if action is None:
                    action = self.resolve_action_by_name(symbol.name)

            # 4. Action name
            if action is None and symbol.action_name is not None:
                if action_overrides:
                    action = action_overrides.get(symbol.action_name, None)

                if action is None:
                    action = self.resolve_action_by_name(symbol.action_name)

                # 5. Try to find action in built-in actions module.
                if action is None:
                    action_name = symbol.action_name
                    if hasattr(actmodule, action_name):
                        action = getattr(actmodule, action_name)

            if symbol.action_name and action is None \
               and fail_on_no_resolve:
                raise ParserInitError(
                    'Action "{}" given for rule "{}" '
                    'doesn\'t exists in parglare common actions and '
                    'is not provided using "actions" parameter.'
                    .format(symbol.action_name, symbol.name))

            if action is not None:
                symbol.action = action

                # Some sanity checks for actions
                if type(symbol.action) is list:
                    if type(symbol) is Terminal:
                        raise ParserInitError(
                            'Cannot use a list of actions for '
                            'terminal "{}".'.format(symbol.name))
                    else:
                        if len(symbol.action) != len(symbol.productions):
                            raise ParserInitError(
                                'Length of list of actions must match the '
                                'number of productions for non-terminal '
                                '"{}".'.format(symbol.name))
            else:
                symbol.action = symbol.grammar_action

    def _connect_override_recognizers(self):
        for term in self.terminals.values():
            if self.recognizers and term.fqn in self.recognizers:
                term.recognizer = self.recognizers[term.fqn]
            else:
                if term.recognizer is None:
                    if not self.recognizers:
                        raise GrammarError(
                            location=term.location,
                            message='Terminal "{}" has no recognizer defined '
                            'and no recognizers are given during grammar '
                            'construction.'.format(term.fqn))
                    else:
                        if term.fqn not in self.recognizers:
                            raise GrammarError(
                                location=term.location,
                                message='Terminal "{}" has no recognizer '
                                'defined.'.format(term.fqn))

    def get_terminal(self, name):
        "Returns terminal with the given fully qualified name or name."
        return self.terminals.get(name)

    def get_nonterminal(self, name):
        "Returns non-terminal with the given fully qualified name or name."
        return self.nonterminals.get(name)

    def get_symbol(self, name):
        "Returns grammar symbol with the given name."
        s = self.get_terminal(name)
        if not s:
            s = self.get_nonterminal(name)
        return s

    def __iter__(self):
        return (s for s in itertools.chain(self.nonterminals.values(),
                                           self.terminals.values())
                if s not in [AUGSYMBOL, STOP])

    def get_production_id(self, name):
        "Returns first production id for the given symbol name"
        for p in self.productions:
            if p.symbol.fqn == name:
                return p.prod_id

    @staticmethod
    def from_struct(grammar_struct, start_symbol=None):
        """
        Construct grammar from the grammar given using a Python structure.
        """
        productions, terminals = create_productions_terminals(grammar_struct)
        return Grammar(productions,
                       terminals=terminals,
                       start_symbol=start_symbol)

    @staticmethod
    def _parse(parse_fun_name, what_to_parse, recognizers=None,
               ignore_case=False, re_flags=re.MULTILINE, debug=False,
               debug_parse=False, debug_colors=False,
               _no_check_recognizers=False):
        from .parser import Context
        context = Context()
        context.extra = extra = GrammarContext()
        extra.re_flags = re_flags
        extra.ignore_case = ignore_case
        extra.debug = debug
        extra.debug_colors = debug_colors
        extra.classes = {}
        extra.inline_terminals = {}
        extra.imported_with = None
        extra.grammar = None
        grammar_parser = get_grammar_parser(debug_parse, debug_colors)
        imports, productions, terminals, classes = \
            getattr(grammar_parser, parse_fun_name)(what_to_parse,
                                                    context=context)
        g = Grammar(productions=productions,
                    terminals=terminals,
                    classes=classes,
                    imports=imports,
                    recognizers=recognizers,
                    file_path=what_to_parse
                    if parse_fun_name == 'parse_file' else None,
                    _no_check_recognizers=_no_check_recognizers)
        termui.colors = debug_colors
        if debug:
            g.print_debug()

        return g

    @staticmethod
    def from_string(grammar_str, **kwargs):
        return Grammar._parse('parse', grammar_str, **kwargs)

    @staticmethod
    def from_file(file_name, **kwargs):
        file_name = path.realpath(file_name)
        return Grammar._parse('parse_file', file_name, **kwargs)

    def print_debug(self):
        a_print("*** GRAMMAR ***", new_line=True)
        h_print("Terminals:")
        prints(" ".join([text(t) for t in self.terminals]))
        h_print("NonTerminals:")
        prints(" ".join([text(n) for n in self.nonterminals]))

        h_print("Productions:")
        for p in self.productions:
            prints(text(p))


class PGFileImport(object):
    """
    Represents import of a grammar file.

    Attributes:
    module_name (str): Name of this import. By default is the name of grammar
        file without .pg extension.
    file_path (str): A canonical full path of the imported .pg file.
    context (Context): The parsing context.
    imported_with (PGFileImport): First import this import is imported from.
        Used for FQN calculation.
    grammar (Grammar): Grammar object under construction.
    pgfile (PGFile instance or None):

    """
    def __init__(self, module_name, file_path, context):
        self.module_name = module_name
        self.file_path = file_path
        self.context = context
        self.imported_with = context.extra.imported_with
        self.grammar = None
        self.pgfile = None

    @property
    def fqn(self):
        "A fully qualified name of the import following the first import path."
        if self.imported_with:
            return "{}.{}".format(self.imported_with.fqn, self.module_name)
        else:
            return self.module_name

    def load_pgfile(self):
        if self.pgfile is None:
            # First search the global registry of imported files.
            if self.file_path in self.grammar.imported_files:
                self.pgfile = self.grammar.imported_files[self.file_path]
            else:
                # If not found construct new PGFile
                from .parser import Context
                context = Context(extra=self.context.extra,
                                  file_name=self.file_path)
                context.extra.inline_terminals = {}
                context.extra.imported_with = self
                imports, productions, terminals, classes = \
                    get_grammar_parser(
                        self.context.extra.debug,
                        self.context.extra.debug_colors).parse_file(
                            self.file_path, context=context)
                self.pgfile = PGFile(productions=productions,
                                     terminals=terminals,
                                     classes=classes,
                                     imports=imports,
                                     grammar=self.grammar,
                                     imported_with=self,
                                     file_path=self.file_path)

    def resolve_symbol_by_name(self, symbol_name, location=None):
        "Resolves symbol from the imported file."

        return self.pgfile.resolve_symbol_by_name(symbol_name, location)

    def resolve_action_by_name(self, action_name):
        "Resolves action from the imported file."

        return self.pgfile.resolve_action_by_name(action_name)


def create_productions_terminals(productions):
    """Creates Production instances from the list of productions given in
    the form:
    [LHS, RHS, optional ASSOC, optional PRIOR].
    Where LHS is grammar symbol and RHS is a list or tuple of grammar
    symbols from the right-hand side of the production.
    """
    gp = []
    inline_terminals = {}
    for p in productions:
        assoc = ASSOC_NONE
        prior = DEFAULT_PRIORITY
        symbol = p[0]
        if not isinstance(symbol, NonTerminal):
            raise GrammarError(
                location=None,
                message="Invalid production symbol '{}' "
                "for production '{}'".format(symbol, text(p)))
        rhs = ProductionRHS(p[1])
        if len(p) > 2:
            assoc = p[2]
        if len(p) > 3:
            prior = p[3]

        # Convert strings to string recognizers
        for idx, t in enumerate(rhs):
            if isinstance(t, text):
                if t not in inline_terminals:
                    inline_terminals[t] = \
                        Terminal(recognizer=StringRecognizer(t), name=t)
                rhs[idx] = Reference(location=None, name=t)
            elif isinstance(t, Terminal):
                if t.name not in inline_terminals:
                    inline_terminals[t.name] = t
                rhs[idx] = Reference(location=None, name=t.name)

        gp.append(Production(symbol, rhs, assoc=assoc, prior=prior))

    return gp, list(inline_terminals.values())


def make_multiplicity_name(symbol_name, multiplicity=None,
                           separator_name=None):
    if multiplicity is None or multiplicity == MULT_ONE:
        return symbol_name
    name_by_mult = {
        MULT_ZERO_OR_MORE: "0",
        MULT_ONE_OR_MORE: "1",
        MULT_OPTIONAL: "opt"
    }
    if multiplicity:
        return "{}_{}{}".format(
            symbol_name, name_by_mult[multiplicity],
            "_{}".format(separator_name) if separator_name else "")


def check_name(context, name):
    """
    Used in actions to check for reserved names usage.
    """

    if name in RESERVED_SYMBOL_NAMES:
        raise GrammarError(
            location=Location(context),
            message='Rule name "{}" is reserved.'.format(name))
    if '.' in name:
        raise GrammarError(
            location=Location(context),
            message='Using dot in names is not allowed.'.format(name))


class GrammarContext:
    def __deepcopy__(self, memo):
        # Use shallow copy for grammar context
        return copy(self)


# Grammar for grammars

(PGFILE,
 IMPORTS,
 IMPORT,
 PRODUCTION_RULES,
 PRODUCTION_RULE,
 PRODUCTION_RULE_WITH_ACTION,
 PRODUCTION_RULE_RHS,
 PRODUCTION,
 TERMINAL_RULES,
 TERMINAL_RULE,
 TERMINAL_RULE_WITH_ACTION,
 PROD_META_DATA,
 PROD_META_DATAS,
 TERM_META_DATA,
 TERM_META_DATAS,
 USER_META_DATA,
 CONST,

 ASSIGNMENT,
 ASSIGNMENTS,
 PLAIN_ASSIGNMENT,
 BOOL_ASSIGNMENT,

 GSYMBOL_REFERENCE,
 OPT_REP_OPERATOR,
 REP_OPERATOR_ZERO,
 REP_OPERATOR_ONE,
 REP_OPERATOR_OPTIONAL,
 OPT_REP_MODIFIERS_EXP,
 OPT_REP_MODIFIERS,
 OPT_REP_MODIFIER,

 GSYMBOL,
 RECOGNIZER,
 LAYOUT,
 LAYOUT_ITEM,
 COMMENT,
 CORNC,
 CORNCS) = [NonTerminal(name) for name in [
     'PGFile',
     'Imports',
     'Import',
     'ProductionRules',
     'ProductionRule',
     'ProductionRuleWithAction',
     'ProductionRuleRHS',
     'Production',
     'TerminalRules',
     'TerminalRule',
     'TerminalRuleWithAction',
     'ProductionMetaData',
     'ProductionMetaDatas',
     'TerminalMetaData',
     'TerminalMetaDatas',
     'UserMetaData',
     'Const',

     'Assignment',
     'Assignments',
     'PlainAssignment',
     'BoolAssignment',

     'GrammarSymbolReference',
     'OptRepeatOperator',
     'RepeatOperatorZero',
     'RepeatOperatorOne',
     'RepeatOperatorOptional',
     'OptionalRepeatModifiersExpression',
     'OptionalRepeatModifiers',
     'OptionalRepeatModifier',

     'GrammarSymbol',
     'Recognizer',
     'LAYOUT',
     'LAYOUT_ITEM',
     'Comment',
     'CORNC',
     'CORNCS']]

pg_terminals = \
    (NAME,
     REGEX_TERM,
     INT_CONST,
     FLOAT_CONST,
     BOOL_CONST,
     STR_CONST,
     ACTION,
     WS,
     COMMENTLINE,
     NOTCOMMENT) = [Terminal(name, RegExRecognizer(regex)) for name, regex in
                    [
                        ('Name', r'[a-zA-Z_][a-zA-Z0-9_\.]*'),
                        ('RegExTerm', r'\/(\\.|[^\/\\])*\/'),
                        ('IntConst', r'\d+'),
                        ('FloatConst',
                         r'''[+-]?(\d+\.\d*|\.\d+)([eE][+-]?\d+)?(?<=[\w\.])(?![\w\.])'''),  # noqa
                        ('BoolConst', r'true|false'),
                        ('StrConst', r'''(?s)('[^'\\]*(?:\\.[^'\\]*)*')|'''
                         r'''("[^"\\]*(?:\\.[^"\\]*)*")'''),
                        ('Action', r'@[a-zA-Z0-9_]+'),
                        ('WS', r'\s+'),
                        ('CommentLine', r'\/\/.*'),
                        ('NotComment', r'((\*[^\/])|[^\s*\/]|\/[^\*])+'),
                    ]]

pg_productions = [
    [PGFILE, [PRODUCTION_RULES, EOF]],
    [PGFILE, [IMPORTS, PRODUCTION_RULES, EOF]],
    [PGFILE, [PRODUCTION_RULES, 'terminals', TERMINAL_RULES, EOF]],
    [PGFILE, [IMPORTS, PRODUCTION_RULES, 'terminals', TERMINAL_RULES, EOF]],
    [PGFILE, ['terminals', TERMINAL_RULES, EOF]],
    [IMPORTS, [IMPORTS, IMPORT]],
    [IMPORTS, [IMPORT]],
    [IMPORT, ['import', STR_CONST, ';']],
    [IMPORT, ['import', STR_CONST, 'as', NAME, ';']],
    [PRODUCTION_RULES, [PRODUCTION_RULES, PRODUCTION_RULE_WITH_ACTION]],
    [PRODUCTION_RULES, [PRODUCTION_RULE_WITH_ACTION]],

    [PRODUCTION_RULE_WITH_ACTION, [ACTION, PRODUCTION_RULE]],
    [PRODUCTION_RULE_WITH_ACTION, [PRODUCTION_RULE]],
    [PRODUCTION_RULE, [NAME, ':', PRODUCTION_RULE_RHS, ';']],
    [PRODUCTION_RULE, [NAME, '{', PROD_META_DATAS, '}', ':',
                       PRODUCTION_RULE_RHS, ';']],
    [PRODUCTION_RULE_RHS, [PRODUCTION_RULE_RHS, '|', PRODUCTION],
     ASSOC_LEFT, 5],
    [PRODUCTION_RULE_RHS, [PRODUCTION], ASSOC_LEFT, 5],
    [PRODUCTION, [ASSIGNMENTS]],
    [PRODUCTION, [ASSIGNMENTS, '{', PROD_META_DATAS, '}']],

    [TERMINAL_RULES, [TERMINAL_RULES, TERMINAL_RULE_WITH_ACTION]],
    [TERMINAL_RULES, [TERMINAL_RULE_WITH_ACTION]],
    [TERMINAL_RULE_WITH_ACTION, [ACTION, TERMINAL_RULE]],
    [TERMINAL_RULE_WITH_ACTION, [TERMINAL_RULE]],
    [TERMINAL_RULE, [NAME, ':', RECOGNIZER, ';'], ASSOC_LEFT, 15],
    [TERMINAL_RULE, [NAME, ':', ';'], ASSOC_LEFT, 15],
    [TERMINAL_RULE, [NAME, ':', RECOGNIZER, '{', TERM_META_DATAS, '}', ';'],
     ASSOC_LEFT, 15],
    [TERMINAL_RULE, [NAME, ':', '{', TERM_META_DATAS, '}', ';'],
     ASSOC_LEFT, 15],

    [PROD_META_DATA, ['left']],
    [PROD_META_DATA, ['reduce']],
    [PROD_META_DATA, ['right']],
    [PROD_META_DATA, ['shift']],
    [PROD_META_DATA, ['dynamic']],
    [PROD_META_DATA, ['nops']],   # no prefer shifts
    [PROD_META_DATA, ['nopse']],  # no prefer shifts over empty
    [PROD_META_DATA, [INT_CONST]],  # priority
    [PROD_META_DATA, [USER_META_DATA]],
    [PROD_META_DATAS, [PROD_META_DATAS, ',', PROD_META_DATA], ASSOC_LEFT],
    [PROD_META_DATAS, [PROD_META_DATA]],

    [TERM_META_DATA, ['prefer']],
    [TERM_META_DATA, ['finish']],
    [TERM_META_DATA, ['nofinish']],
    [TERM_META_DATA, ['dynamic']],
    [TERM_META_DATA, [INT_CONST]],  # priority
    [TERM_META_DATA, [USER_META_DATA]],
    [TERM_META_DATAS, [TERM_META_DATAS, ',', TERM_META_DATA]],
    [TERM_META_DATAS, [TERM_META_DATA]],

    # User custom meta-data
    [USER_META_DATA, [NAME, ':', CONST]],
    [CONST, [INT_CONST]],
    [CONST, [FLOAT_CONST]],
    [CONST, [BOOL_CONST]],
    [CONST, [STR_CONST]],

    # Assignments
    [ASSIGNMENT, [PLAIN_ASSIGNMENT]],
    [ASSIGNMENT, [BOOL_ASSIGNMENT]],
    [ASSIGNMENT, [GSYMBOL_REFERENCE]],
    [ASSIGNMENTS, [ASSIGNMENTS, ASSIGNMENT]],
    [ASSIGNMENTS, [ASSIGNMENT]],
    [PLAIN_ASSIGNMENT, [NAME, '=', GSYMBOL_REFERENCE]],
    [BOOL_ASSIGNMENT, [NAME, '?=', GSYMBOL_REFERENCE]],

    # Regex-like repeat operators
    [GSYMBOL_REFERENCE, [GSYMBOL, OPT_REP_OPERATOR]],
    [OPT_REP_OPERATOR, [REP_OPERATOR_ZERO]],
    [OPT_REP_OPERATOR, [REP_OPERATOR_ONE]],
    [OPT_REP_OPERATOR, [REP_OPERATOR_OPTIONAL]],
    [OPT_REP_OPERATOR, [EMPTY]],
    [REP_OPERATOR_ZERO, ['*', OPT_REP_MODIFIERS_EXP]],
    [REP_OPERATOR_ONE, ['+', OPT_REP_MODIFIERS_EXP]],
    [REP_OPERATOR_OPTIONAL, ['?', OPT_REP_MODIFIERS_EXP]],
    [OPT_REP_MODIFIERS_EXP, ['[', OPT_REP_MODIFIERS, ']']],
    [OPT_REP_MODIFIERS_EXP, [EMPTY]],
    [OPT_REP_MODIFIERS, [OPT_REP_MODIFIERS, ',', OPT_REP_MODIFIER]],
    [OPT_REP_MODIFIERS, [OPT_REP_MODIFIER]],
    [OPT_REP_MODIFIER, [NAME]],

    [GSYMBOL, [NAME]],
    [GSYMBOL, [STR_CONST]],
    [RECOGNIZER, [STR_CONST]],
    [RECOGNIZER, [REGEX_TERM]],

    # Support for comments,
    [LAYOUT, [LAYOUT_ITEM]],
    [LAYOUT, [LAYOUT, LAYOUT_ITEM]],
    [LAYOUT_ITEM, [WS]],
    [LAYOUT_ITEM, [COMMENT]],
    [LAYOUT_ITEM, [EMPTY]],
    [COMMENT, ['/*', CORNCS, '*/']],
    [COMMENT, [COMMENTLINE]],
    [CORNCS, [CORNC]],
    [CORNCS, [CORNCS, CORNC]],
    [CORNCS, [EMPTY]],
    [CORNC, [COMMENT]],
    [CORNC, [NOTCOMMENT]],
    [CORNC, [WS]]
]


grammar_parser = None


def get_grammar_parser(debug, debug_colors):
    global grammar_parser
    if not grammar_parser:
        from parglare import Parser
        grammar_parser = Parser(Grammar.from_struct(pg_productions, PGFILE),
                                actions=pg_actions,
                                debug=debug,
                                debug_colors=debug_colors)
    EMPTY.action = pass_none
    EOF.action = pass_none
    return grammar_parser


def act_pgfile(context, nodes):
    imports, productions, terminals = [], [], []
    while nodes:
        first = nodes.pop(0)
        if first and type(first) is list:
            if type(first[0]) is PGFileImport:
                imports = first
            elif type(first[0]) is Production:
                productions = first
            elif type(first[0]) is Terminal:
                terminals = first

    for terminal in context.extra.inline_terminals.values():
        terminals.append(terminal)

    return [imports, productions, terminals, context.extra.classes]


def act_import(context, nodes):
    if not context.file_name:
        raise GrammarError(location=Location(context),
                           message='Import can be used only for grammars '
                           'defined in files.')
    import_path = nodes[1]
    module_name = nodes[3] if len(nodes) > 3 else None
    if module_name is None:
        module_name = path.splitext(path.basename(import_path))[0]
    if not path.isabs(import_path):
        import_path = path.realpath(path.join(path.dirname(context.file_name),
                                              import_path))
    else:
        import_path = path.realpath(import_path)

    return PGFileImport(module_name, import_path, context)


def act_production_rules(_, nodes):
    e1, e2 = nodes
    e1.extend(e2)
    return e1


def act_production_rule_with_action(_, nodes):
    if len(nodes) > 1:
        action_name, productions = nodes
        # Strip @ char
        action_name = action_name[1:]
        for p in productions:
            p.symbol.action_name = action_name
    else:
        productions = nodes[0]

    return productions


def act_production_rule(context, nodes):
    if len(nodes) == 4:
        # No meta-data
        name, _, rhs_prods, __ = nodes
        rule_meta_datas = {}
    else:
        name, rule_meta_datas, rhs_prods = nodes[0], nodes[2], nodes[5]
        rule_meta_datas = get_production_rule_meta_datas(rule_meta_datas)

    check_name(context, name)

    symbol = NonTerminal(name, location=Location(context),
                         imported_with=context.extra.imported_with,
                         user_meta=rule_meta_datas.get('user_meta', None))

    # Collect all productions for this rule
    prods = []
    attrs = {}
    for prod in rhs_prods:
        assignments, meta_datas = prod
        # Here we know the indexes of assignments
        for idx, a in enumerate(assignments):
            if a.name:
                a.index = idx
        gsymbols = (a.symbol for a in assignments)
        assoc = meta_datas.get('assoc', rule_meta_datas.get('assoc',
                                                            ASSOC_NONE))
        prior = meta_datas.get('priority',
                               rule_meta_datas.get('priority',
                                                   DEFAULT_PRIORITY))
        dynamic = meta_datas.get('dynamic',
                                 rule_meta_datas.get('dynamic', False))
        nops = meta_datas.get('nops',
                              rule_meta_datas.get('nops', False))
        nopse = meta_datas.get('nopse', rule_meta_datas.get('nopse', False))

        # User meta-data if formed by rule-level user meta-data with overrides
        # from production-level user meta-data.
        user_meta = dict(rule_meta_datas.get('user_meta', {}))
        user_meta.update(meta_datas.get('user_meta', {}))
        prods.append(Production(symbol,
                                ProductionRHS(gsymbols),
                                assignments=assignments,
                                assoc=assoc,
                                prior=prior,
                                dynamic=dynamic,
                                nops=nops,
                                nopse=nopse,
                                user_meta=user_meta))

        for a in assignments:
            if a.name:
                attrs[a.name] = PGAttribute(a.name, a.mult, a.symbol_name)
            # TODO: check/handle multiple assignments to the same attribute
            #       If a single production have multiple assignment of the
            #       same attribute, multiplicity must be set to many.

    # If named matches are used create Python class that will be used
    # for object instantiation.
    if attrs:
        class ParglareMetaClass(type):

            def __repr__(cls):
                return '<parglare:{} class at {}>'.format(name, id(cls))

        @add_metaclass(ParglareMetaClass)
        class ParglareClass(object):
            """Dynamically created class. Each parglare rule that uses named
            matches by default uses this action that will create Python object
            of this class.

            Attributes:
                _pg_attrs(dict): A dict of meta-attributes keyed by name.
                    Used by common rules.
                _pg_position(int): A position in the input string where
                    this class is defined.
                _pg_position_end(int): A position in the input string where
                    this class ends.

            """

            _pg_attrs = attrs

            def __init__(self, **attrs):
                for attr_name, attr_value in attrs.items():
                    setattr(self, attr_name, attr_value)

            def __repr__(self):
                if hasattr(self, 'name'):
                    return "<{}:{}>".format(name, self.name)
                else:
                    return "<parglare:{} instance at {}>"\
                        .format(name, hex(id(self)))

        ParglareClass.__name__ = str(symbol.fqn)
        if symbol.fqn in context.extra.classes:
            # If rule has multiple definition merge attributes.
            context.extra.classes[symbol.fqn]._pg_attrs.update(attrs)
        else:
            context.extra.classes[symbol.fqn] = ParglareClass

        symbol.action_name = 'obj'

    return prods


def get_production_rule_meta_datas(raw_meta_datas):
    meta_datas = {}
    for meta_data in raw_meta_datas:
        if meta_data in ['left', 'reduce']:
            meta_datas['assoc'] = ASSOC_LEFT
        elif meta_data in ['right', 'shift']:
            meta_datas['assoc'] = ASSOC_RIGHT
        elif meta_data == 'dynamic':
            meta_datas['dynamic'] = True
        elif meta_data == 'nops':
            meta_datas['nops'] = True
        elif meta_data == 'nopse':
            meta_datas['nopse'] = True
        elif type(meta_data) is int:
            meta_datas['priority'] = meta_data
        else:
            # User meta-data
            assert type(meta_data) is list
            name, _, value = meta_data
            meta_datas.setdefault('user_meta', {})[name] = value
    return meta_datas


def act_production(_, nodes):
    assignments = nodes[0]
    meta_datas = {}
    if len(nodes) > 1:
        meta_datas = get_production_rule_meta_datas(nodes[2])

    return (assignments, meta_datas)


def _set_term_props(term, props):
    for t in props:
        if type(t) is int:
            term.prior = t
        elif type(t) is list:
            # User meta-data
            name, _, value = t
            term.add_user_meta_data(name, value)
        elif t == 'finish':
            term.finish = True
        elif t == 'nofinish':
            term.finish = False
        elif t == 'prefer':
            term.prefer = True
        elif t == 'dynamic':
            term.dynamic = True
        else:
            print(t)
            assert False


def act_term_rule(context, nodes):

    name = nodes[0]
    recognizer = nodes[2]

    check_name(context, name)
    term = Terminal(name, recognizer, location=Location(context),
                    imported_with=context.extra.imported_with)
    if len(nodes) > 4:
        _set_term_props(term, nodes[4])
    return term


def act_term_rule_empty_body(context, nodes):
    name = nodes[0]

    check_name(context, name)
    term = Terminal(name, location=Location(context),
                    imported_with=context.extra.imported_with)
    term.recognizer = None
    if len(nodes) > 3:
        _set_term_props(term, nodes[3])
    return term


def act_term_rule_with_action(context, nodes):
    if len(nodes) > 1:
        action_name, term = nodes
        # Strip @ char
        action_name = action_name[1:]
        term.action_name = action_name
    else:
        term = nodes[0]

    return term


def act_gsymbol_reference(context, nodes):
    """Repetition operators (`*`, `+`, `?`) will create additional productions in
    the grammar with name generated from original symbol name and suffixes:
    - `_0` - for `*`
    - `_1` - for `+`
    - `_opt` - for `?`

    Zero or more produces `one or more` productions and additional productions
    of the form:

    ```
    somerule_0: somerule_1 | EMPTY;
    ```

    In addition if separator is used another suffix is added which is the name
    of the separator rule, for example:

    ```
    spam*[comma] --> spam_0_comma and spam_1_comma
    spam+[comma] --> spam_1_comma
    spam* --> spam_0 and spam_1
    spam? --> spam_opt
    ```

    """
    symbol_ref, rep_op = nodes

    if rep_op:

        if len(rep_op) > 1:
            rep_op, modifiers = rep_op
        else:
            rep_op = rep_op[0]
            modifiers = None

        sep_ref = None
        if modifiers:
            sep_ref = modifiers[1]
            sep_ref = Reference(Location(context), sep_ref)
            symbol_ref.separator = sep_ref

        if rep_op == '*':
            symbol_ref.multiplicity = MULT_ZERO_OR_MORE
        elif rep_op == '+':
            symbol_ref.multiplicity = MULT_ONE_OR_MORE
        else:
            symbol_ref.multiplicity = MULT_OPTIONAL

    return symbol_ref


def act_gsymbol_string_recognizer(context, nodes):
    recognizer = act_recognizer_str(context, nodes)

    terminal_ref = Reference(Location(context), recognizer.name)

    if terminal_ref.name not in context.extra.inline_terminals:
        check_name(context, terminal_ref.name)
        context.extra.inline_terminals[terminal_ref.name] = \
            Terminal(terminal_ref.name, recognizer, location=Location(context))

    return terminal_ref


def act_assignment(_, nodes):
    gsymbol_reference = nodes[0]
    if type(gsymbol_reference) is list:
        # Named match
        name, op, gsymbol_reference = gsymbol_reference
    else:
        name, op = None, None

    return Assignment(name, op, gsymbol_reference)


def act_recognizer_str(context, nodes):
    value = nodes[0]
    value = value.replace(r'\"', '"')\
                 .replace(r"\'", "'")\
                 .replace(r"\\", "\\")\
                 .replace(r"\n", "\n")\
                 .replace(r"\t", "\t")
    return StringRecognizer(value, ignore_case=context.extra.ignore_case)


def act_recognizer_regex(context, nodes):
    value = nodes[0]
    return RegExRecognizer(value, re_flags=context.extra.re_flags,
                           ignore_case=context.extra.ignore_case)


def act_str_term(context, value):
    value = value[1:-1]
    value = value.replace(r"\\", "\\")
    value = value.replace(r"\'", "'")
    return value


def act_regex_term(context, value):
    return value[1:-1]


# pg_actions = {
#     "PGFile": act_pgfile,
#     "Imports": collect,
#     "Import": act_import,

#     "ProductionRules": [act_production_rules, pass_single],
#     'ProductionRule': act_production_rule,
#     'ProductionRuleWithAction': act_production_rule_with_action,
#     'ProductionRuleRHS': collect_sep,
#     'Production': act_production,

#     'TerminalRules': collect,
#     'TerminalRule': [act_term_rule,
#                      act_term_rule_empty_body,
#                      act_term_rule,
#                      act_term_rule_empty_body],
#     'TerminalRuleWithAction': act_term_rule_with_action,

#     "ProductionMetaDatas": collect_sep,
#     "TerminalMetaDatas": collect_sep,

#     "Assignment": act_assignment,
#     "Assignments": collect,

#     'GrammarSymbolReference': act_gsymbol_reference,

#     'GrammarSymbol': [lambda context, nodes: Reference(Location(context),
#                                                        nodes[0]),
#                       act_gsymbol_string_recognizer],

#     'Recognizer': [act_recognizer_str, act_recognizer_regex],

#     'StrConst': act_str_term,
#     'RegExTerm': act_regex_term,

#     # Constants
#     'IntConst': lambda _, value: int(value),
#     'FloatConst': lambda _, value: float(value),
#     'BoolConst': lambda _, value: value and value.lower() == 'true',

# }
