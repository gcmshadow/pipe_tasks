import yaml
import re

import pandas as pd
import numpy as np
import astropy.units as u

from lsst.daf.persistence import doImport
from lsst.daf.butler import DeferredDatasetHandle
from .parquetTable import MultilevelParquetTable


def init_fromDict(initDict, basePath='lsst.pipe.tasks.functors',
                  typeKey='functor', name=None):
    """Initialize an object defined in a dictionary

    The object needs to be importable as
        f'{basePath}.{initDict[typeKey]}'
    The positional and keyword arguments (if any) are contained in
    "args" and "kwargs" entries in the dictionary, respectively.
    This is used in `functors.CompositeFunctor.from_yaml` to initialize
    a composite functor from a specification in a YAML file.

    Parameters
    ----------
    initDict : dictionary
        Dictionary describing object's initialization.  Must contain
        an entry keyed by ``typeKey`` that is the name of the object,
        relative to ``basePath``.
    basePath : str
        Path relative to module in which ``initDict[typeKey]`` is defined.
    typeKey : str
        Key of ``initDict`` that is the name of the object
        (relative to `basePath`).
    """
    initDict = initDict.copy()
    # TO DO: DM-21956 We should be able to define functors outside this module
    pythonType = doImport(f'{basePath}.{initDict.pop(typeKey)}')
    args = []
    if 'args' in initDict:
        args = initDict.pop('args')
        if isinstance(args, str):
            args = [args]
    try:
        element = pythonType(*args, **initDict)
    except Exception as e:
        message = f'Error in constructing functor "{name}" of type {pythonType.__name__} with args: {args}'
        raise type(e)(message, e.args)
    return element


class Functor(object):
    """Define and execute a calculation on a ParquetTable

    The `__call__` method accepts a `ParquetTable` object, and returns the
    result of the calculation as a single column.  Each functor defines what
    columns are needed for the calculation, and only these columns are read
    from the `ParquetTable`.

    The action of  `__call__` consists of two steps: first, loading the
    necessary columns from disk into memory as a `pandas.DataFrame` object;
    and second, performing the computation on this dataframe and returning the
    result.


    To define a new `Functor`, a subclass must define a `_func` method,
    that takes a `pandas.DataFrame` and returns result in a `pandas.Series`.
    In addition, it must define the following attributes

    * `_columns`: The columns necessary to perform the calculation
    * `name`: A name appropriate for a figure axis label
    * `shortname`: A name appropriate for use as a dictionary key

    On initialization, a `Functor` should declare what filter (`filt` kwarg)
    and dataset (e.g. `'ref'`, `'meas'`, `'forced_src'`) it is intended to be
    applied to. This enables the `_get_cols` method to extract the proper
    columns from the parquet file. If not specified, the dataset will fall back
    on the `_defaultDataset`attribute. If filter is not specified and `dataset`
    is anything other than `'ref'`, then an error will be raised when trying to
    perform the calculation.

    As currently implemented, `Functor` is only set up to expect a
    `ParquetTable` of the format of the `deepCoadd_obj` dataset; that is, a
    `MultilevelParquetTable` with the levels of the column index being `filter`,
    `dataset`, and `column`. This is defined in the `_columnLevels` attribute,
    as well as being implicit in the role of the `filt` and `dataset` attributes
    defined at initialization.  In addition, the `_get_cols` method that reads
    the dataframe from the `ParquetTable` will return a dataframe with column
    index levels defined by the `_dfLevels` attribute; by default, this is
    `column`.

    The `_columnLevels` and `_dfLevels` attributes should generally not need to
    be changed, unless `_func` needs columns from multiple filters or datasets
    to do the calculation.
    An example of this is the `lsst.pipe.tasks.functors.Color` functor, for
    which `_dfLevels = ('filter', 'column')`, and `_func` expects the dataframe
    it gets to have those levels in the column index.

    Parameters
    ----------
    filt : str
        Filter upon which to do the calculation

    dataset : str
        Dataset upon which to do the calculation
        (e.g., 'ref', 'meas', 'forced_src').

    """

    _defaultDataset = 'ref'
    _columnLevels = ('filter', 'dataset', 'column')
    _dfLevels = ('column',)
    _defaultNoDup = False

    def __init__(self, filt=None, dataset=None, noDup=None):
        self.filt = filt
        self.dataset = dataset if dataset is not None else self._defaultDataset
        self._noDup = noDup

    @property
    def noDup(self):
        if self._noDup is not None:
            return self._noDup
        else:
            return self._defaultNoDup

    @property
    def columns(self):
        """Columns required to perform calculation
        """
        if not hasattr(self, '_columns'):
            raise NotImplementedError('Must define columns property or _columns attribute')
        return self._columns

    def multilevelColumns(self, parq):
        if not set(parq.columnLevels) == set(self._columnLevels):
            raise ValueError('ParquetTable does not have the expected column levels. '
                             f'Got {parq.columnLevels}; expected {self._columnLevels}.')

        columnDict = {'column': self.columns,
                      'dataset': self.dataset}
        if self.filt is None:
            if 'filter' in parq.columnLevels:
                if self.dataset == 'ref':
                    columnDict['filter'] = parq.columnLevelNames['filter'][0]
                else:
                    raise ValueError(f"'filt' not set for functor {self.name}"
                                     f"(dataset {self.dataset}) "
                                     "and ParquetTable "
                                     "contains multiple filters in column index. "
                                     "Set 'filt' or set 'dataset' to 'ref'.")
        else:
            columnDict['filter'] = self.filt

        return parq._colsFromDict(columnDict)

    def multilevelColumnsGen3(self, parq):
        if not set(parq.columnLevels) == set(self._columnLevels):
            raise ValueError('ParquetTable does not have the expected column levels. '
                             f'Got {parq.columnLevels}; expected {self._columnLevels}.')

        columnDict = {'column': self.columns,
                      'dataset': self.dataset}
        if self.filt is None:
            if 'filter' in parq.columnLevels:
                if self.dataset == 'ref':
                    columnDict['filter'] = parq.columnLevelNames['filter'][0]
                else:
                    raise ValueError(f"'filt' not set for functor {self.name}"
                                     f"(dataset {self.dataset}) "
                                     "and ParquetTable "
                                     "contains multiple filters in column index. "
                                     "Set 'filt' or set 'dataset' to 'ref'.")
        else:
            columnDict['filter'] = self.filt

        return parq._colsFromDict(columnDict)

    def _func(self, df, dropna=True):
        raise NotImplementedError('Must define calculation on dataframe')

    def _get_cols(self, parq):
        """Retrieve dataframe necessary for calculation.

        Returns dataframe upon which `self._func` can act.
        """
        if isinstance(parq, MultilevelParquetTable):
            columns = self.multilevelColumns(parq)
            df = parq.toDataFrame(columns=columns, droplevels=False)
            df = self._setLevels(df)
        elif isinstance(parq, DeferredDatasetHandle):
            import pdb
            pdb.set_trace()
            # We are in _get_cols!
        else:
            columns = self.columns
            df = parq.toDataFrame(columns=columns)

        return df

    def _setLevels(self, df):
        levelsToDrop = [n for n in df.columns.names if n not in self._dfLevels]
        df.columns = df.columns.droplevel(levelsToDrop)
        return df

    def _dropna(self, vals):
        return vals.dropna()

    def __call__(self, parq, dropna=False):
        try:
            df = self._get_cols(parq)
            vals = self._func(df)
        except Exception:
            vals = self.fail(df)
        if dropna:
            vals = self._dropna(vals)

        return vals

    def difference(self, parq1, parq2, **kwargs):
        """Computes difference between functor called on two different ParquetTable objects
        """
        return self(parq1, **kwargs) - self(parq2, **kwargs)

    def fail(self, df):
        return pd.Series(np.full(len(df), np.nan), index=df.index)

    @property
    def name(self):
        """Full name of functor (suitable for figure labels)
        """
        return NotImplementedError

    @property
    def shortname(self):
        """Short name of functor (suitable for column name/dict key)
        """
        return self.name


class CompositeFunctor(Functor):
    """Perform multiple calculations at once on a catalog

    The role of a `CompositeFunctor` is to group together computations from
    multiple functors.  Instead of returning `pandas.Series` a
    `CompositeFunctor` returns a `pandas.Dataframe`, with the column names
    being the keys of `funcDict`.

    The `columns` attribute of a `CompositeFunctor` is the union of all columns
    in all the component functors.

    A `CompositeFunctor` does not use a `_func` method itself; rather,
    when a `CompositeFunctor` is called, all its columns are loaded
    at once, and the resulting dataframe is passed to the `_func` method of each component
    functor.  This has the advantage of only doing I/O (reading from parquet file) once,
    and works because each individual `_func` method of each component functor does not
    care if there are *extra* columns in the dataframe being passed; only that it must contain
    *at least* the `columns` it expects.

    An important and useful class method is `from_yaml`, which takes as argument the path to a YAML
    file specifying a collection of functors.

    Parameters
    ----------
    funcs : `dict` or `list`
        Dictionary or list of functors.  If a list, then it will be converted
        into a dictonary according to the `.shortname` attribute of each functor.

    """
    dataset = None

    def __init__(self, funcs, **kwargs):

        if type(funcs) == dict:
            self.funcDict = funcs
        else:
            self.funcDict = {f.shortname: f for f in funcs}

        self._filt = None

        super().__init__(**kwargs)

    @property
    def filt(self):
        return self._filt

    @filt.setter
    def filt(self, filt):
        if filt is not None:
            for _, f in self.funcDict.items():
                f.filt = filt
        self._filt = filt

    def update(self, new):
        if isinstance(new, dict):
            self.funcDict.update(new)
        elif isinstance(new, CompositeFunctor):
            self.funcDict.update(new.funcDict)
        else:
            raise TypeError('Can only update with dictionary or CompositeFunctor.')

        # Make sure new functors have the same 'filt' set
        if self.filt is not None:
            self.filt = self.filt

    @property
    def columns(self):
        return list(set([x for y in [f.columns for f in self.funcDict.values()] for x in y]))

    def multilevelColumns(self, parq):
        return list(set([x for y in [f.multilevelColumns(parq)
                                     for f in self.funcDict.values()] for x in y]))

    def __call__(self, parq, **kwargs):
        if isinstance(parq, DeferredDatasetHandle):
            columns = parq.get(columns=c

        elif isinstance(parq, MultilevelParquetTable):
            columns = self.multilevelColumns(parq)
            df = parq.toDataFrame(columns=columns, droplevels=False)
            valDict = {}
            for k, f in self.funcDict.items():
                try:
                    subdf = f._setLevels(df[f.multilevelColumns(parq)])
                    valDict[k] = f._func(subdf)
                except Exception:
                    valDict[k] = f.fail(subdf)
        else:
            columns = self.columns
            df = parq.toDataFrame(columns=columns)
            valDict = {k: f._func(df) for k, f in self.funcDict.items()}

        try:
            valDf = pd.concat(valDict, axis=1)
        except TypeError:
            print([(k, type(v)) for k, v in valDict.items()])
            raise

        if kwargs.get('dropna', False):
            valDf = valDf.dropna(how='any')

        return valDf

    @classmethod
    def renameCol(cls, col, renameRules):
        if renameRules is None:
            return col
        for old, new in renameRules:
            if col.startswith(old):
                col = col.replace(old, new)
        return col

    @classmethod
    def from_file(cls, filename, **kwargs):
        with open(filename) as f:
            translationDefinition = yaml.safe_load(f)

        return cls.from_yaml(translationDefinition, **kwargs)

    @classmethod
    def from_yaml(cls, translationDefinition, **kwargs):
        funcs = {}
        for func, val in translationDefinition['funcs'].items():
            funcs[func] = init_fromDict(val, name=func)

        if 'flag_rename_rules' in translationDefinition:
            renameRules = translationDefinition['flag_rename_rules']
        else:
            renameRules = None

        if 'refFlags' in translationDefinition:
            for flag in translationDefinition['refFlags']:
                funcs[cls.renameCol(flag, renameRules)] = Column(flag, dataset='ref')

        if 'flags' in translationDefinition:
            for flag in translationDefinition['flags']:
                funcs[cls.renameCol(flag, renameRules)] = Column(flag, dataset='meas')

        return cls(funcs, **kwargs)


def mag_aware_eval(df, expr):
    """Evaluate an expression on a DataFrame, knowing what the 'mag' function means

    Builds on `pandas.DataFrame.eval`, which parses and executes math on dataframes.

    Parameters
    ----------
    df : pandas.DataFrame
        Dataframe on which to evaluate expression.

    expr : str
        Expression.
    """
    try:
        expr_new = re.sub(r'mag\((\w+)\)', r'-2.5*log(\g<1>)/log(10)', expr)
        val = df.eval(expr_new, truediv=True)
    except Exception:  # Should check what actually gets raised
        expr_new = re.sub(r'mag\((\w+)\)', r'-2.5*log(\g<1>_instFlux)/log(10)', expr)
        val = df.eval(expr_new, truediv=True)
    return val


class CustomFunctor(Functor):
    """Arbitrary computation on a catalog

    Column names (and thus the columns to be loaded from catalog) are found
    by finding all words and trying to ignore all "math-y" words.

    Parameters
    ----------
    expr : str
        Expression to evaluate, to be parsed and executed by `mag_aware_eval`.
    """
    _ignore_words = ('mag', 'sin', 'cos', 'exp', 'log', 'sqrt')

    def __init__(self, expr, **kwargs):
        self.expr = expr
        super().__init__(**kwargs)

    @property
    def name(self):
        return self.expr

    @property
    def columns(self):
        flux_cols = re.findall(r'mag\(\s*(\w+)\s*\)', self.expr)

        cols = [c for c in re.findall(r'[a-zA-Z_]+', self.expr) if c not in self._ignore_words]
        not_a_col = []
        for c in flux_cols:
            if not re.search('_instFlux$', c):
                cols.append(f'{c}_instFlux')
                not_a_col.append(c)
            else:
                cols.append(c)

        return list(set([c for c in cols if c not in not_a_col]))

    def _func(self, df):
        return mag_aware_eval(df, self.expr)


class Column(Functor):
    """Get column with specified name
    """

    def __init__(self, col, **kwargs):
        self.col = col
        super().__init__(**kwargs)

    @property
    def name(self):
        return self.col

    @property
    def columns(self):
        return [self.col]

    def _func(self, df):
        return df[self.col]


class Index(Functor):
    """Return the value of the index for each object
    """

    columns = ['coord_ra']  # just a dummy; something has to be here
    _defaultDataset = 'ref'
    _defaultNoDup = True

    def _func(self, df):
        return pd.Series(df.index, index=df.index)


class IDColumn(Column):
    col = 'id'
    _allow_difference = False
    _defaultNoDup = True

    def _func(self, df):
        return pd.Series(df.index, index=df.index)


class FootprintNPix(Column):
    col = 'base_Footprint_nPix'


class CoordColumn(Column):
    """Base class for coordinate column, in degrees
    """
    _radians = True

    def __init__(self, col, **kwargs):
        super().__init__(col, **kwargs)

    def _func(self, df):
        # Must not modify original column in case that column is used by another functor
        output = df[self.col] * 180 / np.pi if self._radians else df[self.col]
        return output


class RAColumn(CoordColumn):
    """Right Ascension, in degrees
    """
    name = 'RA'
    _defaultNoDup = True

    def __init__(self, **kwargs):
        super().__init__('coord_ra', **kwargs)

    def __call__(self, catalog, **kwargs):
        return super().__call__(catalog, **kwargs)


class DecColumn(CoordColumn):
    """Declination, in degrees
    """
    name = 'Dec'
    _defaultNoDup = True

    def __init__(self, **kwargs):
        super().__init__('coord_dec', **kwargs)

    def __call__(self, catalog, **kwargs):
        return super().__call__(catalog, **kwargs)


def fluxName(col):
    if not col.endswith('_instFlux'):
        col += '_instFlux'
    return col


def fluxErrName(col):
    if not col.endswith('_instFluxErr'):
        col += '_instFluxErr'
    return col


class Mag(Functor):
    """Compute calibrated magnitude

    Takes a `calib` argument, which returns the flux at mag=0
    as `calib.getFluxMag0()`.  If not provided, then the default
    `fluxMag0` is 63095734448.0194, which is default for HSC.
    This default should be removed in DM-21955

    This calculation hides warnings about invalid values and dividing by zero.

    As for all functors, a `dataset` and `filt` kwarg should be provided upon
    initialization.  Unlike the default `Functor`, however, the default dataset
    for a `Mag` is `'meas'`, rather than `'ref'`.

    Parameters
    ----------
    col : `str`
        Name of flux column from which to compute magnitude.  Can be parseable
        by `lsst.pipe.tasks.functors.fluxName` function---that is, you can pass
        `'modelfit_CModel'` instead of `'modelfit_CModel_instFlux'`) and it will
        understand.
    calib : `lsst.afw.image.calib.Calib` (optional)
        Object that knows zero point.
    """
    _defaultDataset = 'meas'

    def __init__(self, col, calib=None, **kwargs):
        self.col = fluxName(col)
        self.calib = calib
        if calib is not None:
            self.fluxMag0 = calib.getFluxMag0()[0]
        else:
            # TO DO: DM-21955 Replace hard coded photometic calibration values
            self.fluxMag0 = 63095734448.0194

        super().__init__(**kwargs)

    @property
    def columns(self):
        return [self.col]

    def _func(self, df):
        with np.warnings.catch_warnings():
            np.warnings.filterwarnings('ignore', r'invalid value encountered')
            np.warnings.filterwarnings('ignore', r'divide by zero')
            return -2.5*np.log10(df[self.col] / self.fluxMag0)

    @property
    def name(self):
        return f'mag_{self.col}'


class MagErr(Mag):
    """Compute calibrated magnitude uncertainty

    Takes the same `calib` object as `lsst.pipe.tasks.functors.Mag`.

    Parameters
    col : `str`
        Name of flux column
    calib : `lsst.afw.image.calib.Calib` (optional)
        Object that knows zero point.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.calib is not None:
            self.fluxMag0Err = self.calib.getFluxMag0()[1]
        else:
            self.fluxMag0Err = 0.

    @property
    def columns(self):
        return [self.col, self.col + 'Err']

    def _func(self, df):
        with np.warnings.catch_warnings():
            np.warnings.filterwarnings('ignore', r'invalid value encountered')
            np.warnings.filterwarnings('ignore', r'divide by zero')
            fluxCol, fluxErrCol = self.columns
            x = df[fluxErrCol] / df[fluxCol]
            y = self.fluxMag0Err / self.fluxMag0
            magErr = (2.5 / np.log(10.)) * np.sqrt(x*x + y*y)
            return magErr

    @property
    def name(self):
        return super().name + '_err'


class NanoMaggie(Mag):
    """
    """

    def _func(self, df):
        return (df[self.col] / self.fluxMag0) * 1e9


class MagDiff(Functor):
    _defaultDataset = 'meas'

    """Functor to calculate magnitude difference"""

    def __init__(self, col1, col2, **kwargs):
        self.col1 = fluxName(col1)
        self.col2 = fluxName(col2)
        super().__init__(**kwargs)

    @property
    def columns(self):
        return [self.col1, self.col2]

    def _func(self, df):
        with np.warnings.catch_warnings():
            np.warnings.filterwarnings('ignore', r'invalid value encountered')
            np.warnings.filterwarnings('ignore', r'divide by zero')
            return -2.5*np.log10(df[self.col1]/df[self.col2])

    @property
    def name(self):
        return f'(mag_{self.col1} - mag_{self.col2})'

    @property
    def shortname(self):
        return f'magDiff_{self.col1}_{self.col2}'


class Color(Functor):
    """Compute the color between two filters

    Computes color by initializing two different `Mag`
    functors based on the `col` and filters provided, and
    then returning the difference.

    This is enabled by the `_func` expecting a dataframe with a
    multilevel column index, with both `'filter'` and `'column'`,
    instead of just `'column'`, which is the `Functor` default.
    This is controlled by the `_dfLevels` attribute.

    Also of note, the default dataset for `Color` is `forced_src'`,
    whereas for `Mag` it is `'meas'`.

    Parameters
    ----------
    col : str
        Name of flux column from which to compute; same as would be passed to
        `lsst.pipe.tasks.functors.Mag`.

    filt2, filt1 : str
        Filters from which to compute magnitude difference.
        Color computed is `Mag(filt2) - Mag(filt1)`.
    """
    _defaultDataset = 'forced_src'
    _dfLevels = ('filter', 'column')
    _defaultNoDup = True

    def __init__(self, col, filt2, filt1, **kwargs):
        self.col = fluxName(col)
        if filt2 == filt1:
            raise RuntimeError("Cannot compute Color for %s: %s - %s " % (col, filt2, filt1))
        self.filt2 = filt2
        self.filt1 = filt1

        self.mag2 = Mag(col, filt=filt2, **kwargs)
        self.mag1 = Mag(col, filt=filt1, **kwargs)

        super().__init__(**kwargs)

    @property
    def filt(self):
        return None

    @filt.setter
    def filt(self, filt):
        pass

    def _func(self, df):
        mag2 = self.mag2._func(df[self.filt2])
        mag1 = self.mag1._func(df[self.filt1])
        return mag2 - mag1

    @property
    def columns(self):
        return [self.mag1.col, self.mag2.col]

    def multilevelColumns(self, parq):
        return [(self.dataset, self.filt1, self.col),
                (self.dataset, self.filt2, self.col)]

    @property
    def name(self):
        return f'{self.filt2} - {self.filt1} ({self.col})'

    @property
    def shortname(self):
        return f"{self.col}_{self.filt2.replace('-', '')}m{self.filt1.replace('-', '')}"


class Labeller(Functor):
    """Main function of this subclass is to override the dropna=True
    """
    _null_label = 'null'
    _allow_difference = False
    name = 'label'
    _force_str = False

    def __call__(self, parq, dropna=False, **kwargs):
        return super().__call__(parq, dropna=False, **kwargs)


class StarGalaxyLabeller(Labeller):
    _columns = ["base_ClassificationExtendedness_value"]
    _column = "base_ClassificationExtendedness_value"

    def _func(self, df):
        x = df[self._columns][self._column]
        mask = x.isnull()
        test = (x < 0.5).astype(int)
        test = test.mask(mask, 2)

        # TODO: DM-21954 Look into veracity of inline comment below
        # are these backwards?
        categories = ['galaxy', 'star', self._null_label]
        label = pd.Series(pd.Categorical.from_codes(test, categories=categories),
                          index=x.index, name='label')
        if self._force_str:
            label = label.astype(str)
        return label


class NumStarLabeller(Labeller):
    _columns = ['numStarFlags']
    labels = {"star": 0, "maybe": 1, "notStar": 2}

    def _func(self, df):
        x = df[self._columns][self._columns[0]]

        # Number of filters
        n = len(x.unique()) - 1

        labels = ['noStar', 'maybe', 'star']
        label = pd.Series(pd.cut(x, [-1, 0, n-1, n], labels=labels),
                          index=x.index, name='label')

        if self._force_str:
            label = label.astype(str)

        return label


class DeconvolvedMoments(Functor):
    name = 'Deconvolved Moments'
    shortname = 'deconvolvedMoments'
    _columns = ("ext_shapeHSM_HsmSourceMoments_xx",
                "ext_shapeHSM_HsmSourceMoments_yy",
                "base_SdssShape_xx", "base_SdssShape_yy",
                "ext_shapeHSM_HsmPsfMoments_xx",
                "ext_shapeHSM_HsmPsfMoments_yy")

    def _func(self, df):
        """Calculate deconvolved moments"""
        if "ext_shapeHSM_HsmSourceMoments_xx" in df.columns:  # _xx added by tdm
            hsm = df["ext_shapeHSM_HsmSourceMoments_xx"] + df["ext_shapeHSM_HsmSourceMoments_yy"]
        else:
            hsm = np.ones(len(df))*np.nan
        sdss = df["base_SdssShape_xx"] + df["base_SdssShape_yy"]
        if "ext_shapeHSM_HsmPsfMoments_xx" in df.columns:
            psf = df["ext_shapeHSM_HsmPsfMoments_xx"] + df["ext_shapeHSM_HsmPsfMoments_yy"]
        else:
            # LSST does not have shape.sdss.psf.  Could instead add base_PsfShape to catalog using
            # exposure.getPsf().computeShape(s.getCentroid()).getIxx()
            # raise TaskError("No psf shape parameter found in catalog")
            raise RuntimeError('No psf shape parameter found in catalog')

        return hsm.where(np.isfinite(hsm), sdss) - psf


class SdssTraceSize(Functor):
    """Functor to calculate SDSS trace radius size for sources"""
    name = "SDSS Trace Size"
    shortname = 'sdssTrace'
    _columns = ("base_SdssShape_xx", "base_SdssShape_yy")

    def _func(self, df):
        srcSize = np.sqrt(0.5*(df["base_SdssShape_xx"] + df["base_SdssShape_yy"]))
        return srcSize


class PsfSdssTraceSizeDiff(Functor):
    """Functor to calculate SDSS trace radius size difference (%) between object and psf model"""
    name = "PSF - SDSS Trace Size"
    shortname = 'psf_sdssTrace'
    _columns = ("base_SdssShape_xx", "base_SdssShape_yy",
                "base_SdssShape_psf_xx", "base_SdssShape_psf_yy")

    def _func(self, df):
        srcSize = np.sqrt(0.5*(df["base_SdssShape_xx"] + df["base_SdssShape_yy"]))
        psfSize = np.sqrt(0.5*(df["base_SdssShape_psf_xx"] + df["base_SdssShape_psf_yy"]))
        sizeDiff = 100*(srcSize - psfSize)/(0.5*(srcSize + psfSize))
        return sizeDiff


class HsmTraceSize(Functor):
    """Functor to calculate HSM trace radius size for sources"""
    name = 'HSM Trace Size'
    shortname = 'hsmTrace'
    _columns = ("ext_shapeHSM_HsmSourceMoments_xx",
                "ext_shapeHSM_HsmSourceMoments_yy")

    def _func(self, df):
        srcSize = np.sqrt(0.5*(df["ext_shapeHSM_HsmSourceMoments_xx"]
                               + df["ext_shapeHSM_HsmSourceMoments_yy"]))
        return srcSize


class PsfHsmTraceSizeDiff(Functor):
    """Functor to calculate HSM trace radius size difference (%) between object and psf model"""
    name = 'PSF - HSM Trace Size'
    shortname = 'psf_HsmTrace'
    _columns = ("ext_shapeHSM_HsmSourceMoments_xx",
                "ext_shapeHSM_HsmSourceMoments_yy",
                "ext_shapeHSM_HsmPsfMoments_xx",
                "ext_shapeHSM_HsmPsfMoments_yy")

    def _func(self, df):
        srcSize = np.sqrt(0.5*(df["ext_shapeHSM_HsmSourceMoments_xx"]
                               + df["ext_shapeHSM_HsmSourceMoments_yy"]))
        psfSize = np.sqrt(0.5*(df["ext_shapeHSM_HsmPsfMoments_xx"]
                               + df["ext_shapeHSM_HsmPsfMoments_yy"]))
        sizeDiff = 100*(srcSize - psfSize)/(0.5*(srcSize + psfSize))
        return sizeDiff


class HsmFwhm(Functor):
    name = 'HSM Psf FWHM'
    _columns = ('ext_shapeHSM_HsmPsfMoments_xx', 'ext_shapeHSM_HsmPsfMoments_yy')
    # TODO: DM-21403 pixel scale should be computed from the CD matrix or transform matrix
    pixelScale = 0.168
    SIGMA2FWHM = 2*np.sqrt(2*np.log(2))

    def _func(self, df):
        return self.pixelScale*self.SIGMA2FWHM*np.sqrt(
            0.5*(df['ext_shapeHSM_HsmPsfMoments_xx'] + df['ext_shapeHSM_HsmPsfMoments_yy']))


class E1(Functor):
    name = "Distortion Ellipticity (e1)"
    shortname = "Distortion"

    def __init__(self, colXX, colXY, colYY, **kwargs):
        self.colXX = colXX
        self.colXY = colXY
        self.colYY = colYY
        self._columns = [self.colXX, self.colXY, self.colYY]
        super().__init__(**kwargs)

    @property
    def columns(self):
        return [self.colXX, self.colXY, self.colYY]

    def _func(self, df):
        return df[self.colXX] - df[self.colYY] / (df[self.colXX] + df[self.colYY])


class E2(Functor):
    name = "Ellipticity e2"

    def __init__(self, colXX, colXY, colYY, **kwargs):
        self.colXX = colXX
        self.colXY = colXY
        self.colYY = colYY
        super().__init__(**kwargs)

    @property
    def columns(self):
        return [self.colXX, self.colXY, self.colYY]

    def _func(self, df):
        return 2*df[self.colXY] / (df[self.colXX] + df[self.colYY])


class RadiusFromQuadrupole(Functor):

    def __init__(self, colXX, colXY, colYY, **kwargs):
        self.colXX = colXX
        self.colXY = colXY
        self.colYY = colYY
        super().__init__(**kwargs)

    @property
    def columns(self):
        return [self.colXX, self.colXY, self.colYY]

    def _func(self, df):
        return (df[self.colXX]*df[self.colYY] - df[self.colXY]**2)**0.25


class LocalWcs(Functor):
    """Computations using the stored localWcs.
    """
    name = "LocalWcsOperations"

    def __init__(self,
                 colCD_1_1,
                 colCD_1_2,
                 colCD_2_1,
                 colCD_2_2,
                 **kwargs):
        self.colCD_1_1 = colCD_1_1
        self.colCD_1_2 = colCD_1_2
        self.colCD_2_1 = colCD_2_1
        self.colCD_2_2 = colCD_2_2
        super().__init__(**kwargs)

    def computeDeltaRaDec(self, x, y, cd11, cd12, cd21, cd22):
        """Compute the distance on the sphere from x2, y1 to x1, y1.

        Parameters
        ----------
        x : `pandas.Series`
            X pixel coordinate.
        y : `pandas.Series`
            Y pixel coordinate.
        cd11 : `pandas.Series`
            [1, 1] element of the local Wcs affine transform.
        cd11 : `pandas.Series`
            [1, 1] element of the local Wcs affine transform.
        cd12 : `pandas.Series`
            [1, 2] element of the local Wcs affine transform.
        cd21 : `pandas.Series`
            [2, 1] element of the local Wcs affine transform.
        cd22 : `pandas.Series`
            [2, 2] element of the local Wcs affine transform.

        Returns
        -------
        raDecTuple : tuple
            RA and dec conversion of x and y given the local Wcs. Returned
            units are in radians.

        """
        return (x * cd11 + y * cd12, x * cd21 + y * cd22)

    def computeSkySeperation(self, ra1, dec1, ra2, dec2):
        """Compute the local pixel scale conversion.

        Parameters
        ----------
        ra1 : `pandas.Series`
            Ra of the first coordinate in radians.
        dec1 : `pandas.Series`
            Dec of the first coordinate in radians.
        ra2 : `pandas.Series`
            Ra of the second coordinate in radians.
        dec2 : `pandas.Series`
            Dec of the second coordinate in radians.

        Returns
        -------
        dist : `pandas.Series`
            Distance on the sphere in radians.
        """
        deltaDec = dec2 - dec1
        deltaRa = ra2 - ra1
        return 2 * np.arcsin(
            np.sqrt(
                np.sin(deltaDec / 2) ** 2
                + np.cos(dec2) * np.cos(dec1) * np.sin(deltaRa / 2) ** 2))

    def getSkySeperationFromPixel(self, x1, y1, x2, y2, cd11, cd12, cd21, cd22):
        """Compute the distance on the sphere from x2, y1 to x1, y1.

        Parameters
        ----------
        x1 : `pandas.Series`
            X pixel coordinate.
        y1 : `pandas.Series`
            Y pixel coordinate.
        x2 : `pandas.Series`
            X pixel coordinate.
        y2 : `pandas.Series`
            Y pixel coordinate.
        cd11 : `pandas.Series`
            [1, 1] element of the local Wcs affine transform.
        cd11 : `pandas.Series`
            [1, 1] element of the local Wcs affine transform.
        cd12 : `pandas.Series`
            [1, 2] element of the local Wcs affine transform.
        cd21 : `pandas.Series`
            [2, 1] element of the local Wcs affine transform.
        cd22 : `pandas.Series`
            [2, 2] element of the local Wcs affine transform.

        Returns
        -------
        Distance : `pandas.Series`
            Arcseconds per pixel at the location of the local WC
        """
        ra1, dec1 = self.computeDeltaRaDec(x1, y1, cd11, cd12, cd21, cd22)
        ra2, dec2 = self.computeDeltaRaDec(x2, y2, cd11, cd12, cd21, cd22)
        # Great circle distance for small separations.
        return self.computeSkySeperation(ra1, dec1, ra2, dec2)


class ComputePixelScale(LocalWcs):
    """Compute the local pixel scale from the stored CDMatrix.
    """
    name = "PixelScale"

    @property
    def columns(self):
        return [self.colCD_1_1,
                self.colCD_1_2,
                self.colCD_2_1,
                self.colCD_2_2]

    def pixelScaleArcseconds(self, cd11, cd12, cd21, cd22):
        """Compute the local pixel to scale conversion in arcseconds.

        Parameters
        ----------
        cd11 : `pandas.Series`
            [1, 1] element of the local Wcs affine transform in radians.
        cd11 : `pandas.Series`
            [1, 1] element of the local Wcs affine transform in radians.
        cd12 : `pandas.Series`
            [1, 2] element of the local Wcs affine transform in radians.
        cd21 : `pandas.Series`
            [2, 1] element of the local Wcs affine transform in radians.
        cd22 : `pandas.Series`
            [2, 2] element of the local Wcs affine transform in radians.

        Returns
        -------
        pixScale : `pandas.Series`
            Arcseconds per pixel at the location of the local WC
        """
        return 3600 * np.degrees(np.sqrt(np.fabs(cd11 * cd22 - cd12 * cd21)))

    def _func(self, df):
        return self.pixelScaleArcseconds(df[self.colCD_1_1],
                                         df[self.colCD_1_2],
                                         df[self.colCD_2_1],
                                         df[self.colCD_2_2])


class ConvertPixelToArcseconds(ComputePixelScale):
    """Convert a value in units pixels to units arcseconds.
    """

    def __init__(self,
                 col,
                 colCD_1_1,
                 colCD_1_2,
                 colCD_2_1,
                 colCD_2_2,
                 **kwargs):
        self.col = col
        super().__init__(colCD_1_1,
                         colCD_1_2,
                         colCD_2_1,
                         colCD_2_2,
                         **kwargs)

    @property
    def name(self):
        return f"{self.col}_asArcseconds"

    @property
    def columns(self):
        return [self.col,
                self.colCD_1_1,
                self.colCD_1_2,
                self.colCD_2_1,
                self.colCD_2_2]

    def _func(self, df):
        return df[self.col] * self.pixelScaleArcseconds(df[self.colCD_1_1],
                                                        df[self.colCD_1_2],
                                                        df[self.colCD_2_1],
                                                        df[self.colCD_2_2])


class ReferenceBand(Functor):
    name = 'Reference Band'
    shortname = 'refBand'

    @property
    def columns(self):
        return ["merge_measurement_i",
                "merge_measurement_r",
                "merge_measurement_z",
                "merge_measurement_y",
                "merge_measurement_g"]

    def _func(self, df):
        def getFilterAliasName(row):
            # get column name with the max value (True > False)
            colName = row.idxmax()
            return colName.replace('merge_measurement_', '')

        return df[self.columns].apply(getFilterAliasName, axis=1)


class Photometry(Functor):
    # AB to NanoJansky (3631 Jansky)
    AB_FLUX_SCALE = (0 * u.ABmag).to_value(u.nJy)
    LOG_AB_FLUX_SCALE = 12.56
    FIVE_OVER_2LOG10 = 1.085736204758129569
    # TO DO: DM-21955 Replace hard coded photometic calibration values
    COADD_ZP = 27

    def __init__(self, colFlux, colFluxErr=None, calib=None, **kwargs):
        self.vhypot = np.vectorize(self.hypot)
        self.col = colFlux
        self.colFluxErr = colFluxErr

        self.calib = calib
        if calib is not None:
            self.fluxMag0, self.fluxMag0Err = calib.getFluxMag0()
        else:
            self.fluxMag0 = 1./np.power(10, -0.4*self.COADD_ZP)
            self.fluxMag0Err = 0.

        super().__init__(**kwargs)

    @property
    def columns(self):
        return [self.col]

    @property
    def name(self):
        return f'mag_{self.col}'

    @classmethod
    def hypot(cls, a, b):
        if np.abs(a) < np.abs(b):
            a, b = b, a
        if a == 0.:
            return 0.
        q = b/a
        return np.abs(a) * np.sqrt(1. + q*q)

    def dn2flux(self, dn, fluxMag0):
        return self.AB_FLUX_SCALE * dn / fluxMag0

    def dn2mag(self, dn, fluxMag0):
        with np.warnings.catch_warnings():
            np.warnings.filterwarnings('ignore', r'invalid value encountered')
            np.warnings.filterwarnings('ignore', r'divide by zero')
            return -2.5 * np.log10(dn/fluxMag0)

    def dn2fluxErr(self, dn, dnErr, fluxMag0, fluxMag0Err):
        retVal = self.vhypot(dn * fluxMag0Err, dnErr * fluxMag0)
        retVal *= self.AB_FLUX_SCALE / fluxMag0 / fluxMag0
        return retVal

    def dn2MagErr(self, dn, dnErr, fluxMag0, fluxMag0Err):
        retVal = self.dn2fluxErr(dn, dnErr, fluxMag0, fluxMag0Err) / self.dn2flux(dn, fluxMag0)
        return self.FIVE_OVER_2LOG10 * retVal


class NanoJansky(Photometry):
    def _func(self, df):
        return self.dn2flux(df[self.col], self.fluxMag0)


class NanoJanskyErr(Photometry):
    @property
    def columns(self):
        return [self.col, self.colFluxErr]

    def _func(self, df):
        retArr = self.dn2fluxErr(df[self.col], df[self.colFluxErr], self.fluxMag0, self.fluxMag0Err)
        return pd.Series(retArr, index=df.index)


class Magnitude(Photometry):
    def _func(self, df):
        return self.dn2mag(df[self.col], self.fluxMag0)


class MagnitudeErr(Photometry):
    @property
    def columns(self):
        return [self.col, self.colFluxErr]

    def _func(self, df):
        retArr = self.dn2MagErr(df[self.col], df[self.colFluxErr], self.fluxMag0, self.fluxMag0Err)
        return pd.Series(retArr, index=df.index)


class LocalPhotometry(Functor):
    """Base class for calibrating the specified instrument flux column using
    the local photometric calibration.

    Parameters
    ----------
    instFluxCol : `str`
        Name of the instrument flux column.
    instFluxErrCol : `str`
        Name of the assocated error columns for ``instFluxCol``.
    photoCalibCol : `str`
        Name of local calibration column.
    photoCalibErrCol : `str`
        Error associated with ``photoCalibCol``

    See also
    --------
    LocalPhotometry
    LocalNanojansky
    LocalNanojanskyErr
    LocalMagnitude
    LocalMagnitudeErr
    """
    logNJanskyToAB = (1 * u.nJy).to_value(u.ABmag)

    def __init__(self,
                 instFluxCol,
                 instFluxErrCol,
                 photoCalibCol,
                 photoCalibErrCol,
                 **kwargs):
        self.instFluxCol = instFluxCol
        self.instFluxErrCol = instFluxErrCol
        self.photoCalibCol = photoCalibCol
        self.photoCalibErrCol = photoCalibErrCol
        super().__init__(**kwargs)

    def instFluxToNanojansky(self, instFlux, localCalib):
        """Convert instrument flux to nanojanskys.

        Parameters
        ----------
        instFlux : `numpy.ndarray` or `pandas.Series`
            Array of instrument flux measurements
        localCalib : `numpy.ndarray` or `pandas.Series`
            Array of local photometric calibration estimates.

        Returns
        -------
        calibFlux : `numpy.ndarray` or `pandas.Series`
            Array of calibrated flux measurements.
        """
        return instFlux * localCalib

    def instFluxErrToNanojanskyErr(self, instFlux, instFluxErr, localCalib, localCalibErr):
        """Convert instrument flux to nanojanskys.

        Parameters
        ----------
        instFlux : `numpy.ndarray` or `pandas.Series`
            Array of instrument flux measurements
        instFluxErr : `numpy.ndarray` or `pandas.Series`
            Errors on associated ``instFlux`` values
        localCalib : `numpy.ndarray` or `pandas.Series`
            Array of local photometric calibration estimates.
        localCalibErr : `numpy.ndarray` or `pandas.Series`
           Errors on associated ``localCalib`` values

        Returns
        -------
        calibFluxErr : `numpy.ndarray` or `pandas.Series`
            Errors on calibrated flux measurements.
        """
        return np.hypot(instFluxErr * localCalib, instFlux * localCalibErr)

    def instFluxToMagnitude(self, instFlux, localCalib):
        """Convert instrument flux to nanojanskys.

        Parameters
        ----------
        instFlux : `numpy.ndarray` or `pandas.Series`
            Array of instrument flux measurements
        localCalib : `numpy.ndarray` or `pandas.Series`
            Array of local photometric calibration estimates.

        Returns
        -------
        calibMag : `numpy.ndarray` or `pandas.Series`
            Array of calibrated AB magnitudes.
        """
        return -2.5 * np.log10(self.instFluxToNanojansky(instFlux, localCalib)) + self.logNJanskyToAB

    def instFluxErrToMagnitudeErr(self, instFlux, instFluxErr, localCalib, localCalibErr):
        """Convert instrument flux err to nanojanskys.

        Parameters
        ----------
        instFlux : `numpy.ndarray` or `pandas.Series`
            Array of instrument flux measurements
        instFluxErr : `numpy.ndarray` or `pandas.Series`
            Errors on associated ``instFlux`` values
        localCalib : `numpy.ndarray` or `pandas.Series`
            Array of local photometric calibration estimates.
        localCalibErr : `numpy.ndarray` or `pandas.Series`
           Errors on associated ``localCalib`` values

        Returns
        -------
        calibMagErr: `numpy.ndarray` or `pandas.Series`
            Error on calibrated AB magnitudes.
        """
        err = self.instFluxErrToNanojanskyErr(instFlux, instFluxErr, localCalib, localCalibErr)
        return 2.5 / np.log(10) * err / self.instFluxToNanojansky(instFlux, instFluxErr)


class LocalNanojansky(LocalPhotometry):
    """Compute calibrated fluxes using the local calibration value.

    See also
    --------
    LocalNanojansky
    LocalNanojanskyErr
    LocalMagnitude
    LocalMagnitudeErr
    """

    @property
    def columns(self):
        return [self.instFluxCol, self.photoCalibCol]

    @property
    def name(self):
        return f'flux_{self.instFluxCol}'

    def _func(self, df):
        return self.instFluxToNanojansky(df[self.instFluxCol], df[self.photoCalibCol])


class LocalNanojanskyErr(LocalPhotometry):
    """Compute calibrated flux errors using the local calibration value.

    See also
    --------
    LocalNanojansky
    LocalNanojanskyErr
    LocalMagnitude
    LocalMagnitudeErr
    """

    @property
    def columns(self):
        return [self.instFluxCol, self.instFluxErrCol,
                self.photoCalibCol, self.photoCalibErrCol]

    @property
    def name(self):
        return f'fluxErr_{self.instFluxCol}'

    def _func(self, df):
        return self.instFluxErrToNanojanskyErr(df[self.instFluxCol], df[self.instFluxErrCol],
                                               df[self.photoCalibCol], df[self.photoCalibErrCol])


class LocalMagnitude(LocalPhotometry):
    """Compute calibrated AB magnitudes using the local calibration value.

    See also
    --------
    LocalNanojansky
    LocalNanojanskyErr
    LocalMagnitude
    LocalMagnitudeErr
    """

    @property
    def columns(self):
        return [self.instFluxCol, self.photoCalibCol]

    @property
    def name(self):
        return f'mag_{self.instFluxCol}'

    def _func(self, df):
        return self.instFluxToMagnitude(df[self.instFluxCol],
                                        df[self.photoCalibCol])


class LocalMagnitudeErr(LocalPhotometry):
    """Compute calibrated AB magnitude errors using the local calibration value.

    See also
    --------
    LocalNanojansky
    LocalNanojanskyErr
    LocalMagnitude
    LocalMagnitudeErr
    """

    @property
    def columns(self):
        return [self.instFluxCol, self.instFluxErrCol,
                self.photoCalibCol, self.photoCalibErrCol]

    @property
    def name(self):
        return f'magErr_{self.instFluxCol}'

    def _func(self, df):
        return self.instFluxErrToMagnitudeErr(df[self.instFluxCol],
                                              df[self.instFluxErrCol],
                                              df[self.photoCalibCol],
                                              df[self.photoCalibErrCol])
