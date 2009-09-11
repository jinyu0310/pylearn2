import sys
import elemwise_cgen as cgen

import numpy
from theano import gof
from theano.gof import Op, Apply
from theano import scalar
from theano.scalar import Scalar
from theano import printing
from theano.printing import pprint
from theano.gof.python25 import all, any
from copy import copy, deepcopy


# tensor depends on elemwise to provide definitions for several ops
# but elemwise needs to make TensorType instances, so we have these as
# placeholders and the tensor module fills them
def as_tensor_variable(data):
    raise Exception("Circular dependencies prevent using this here. import tensor before elemwise")

def TensorType(*inputs, **kwargs):
    raise Exception("Circular dependencies prevent using this here. import tensor before elemwise")

def TensorVariable(*inputs, **kwargs):
    raise Exception("Circular dependencies prevent using this here. import tensor before elemwise")

def TensorConstant(*inputs, **kwargs):
    raise Exception("Circular dependencies prevent using this here. import tensor before elemwise")


##################
### DimShuffle ###
##################

class DimShuffle(Op):
    """
    Allows to reorder the dimensions of a tensor or insert or remove
    broadcastable dimensions.

    In the following examples, 'x' means that we insert a broadcastable
    dimension and a numerical index represents the dimension of the same
    rank in the tensor passed to perform.

    Examples:
      DimShuffle((False, False, False), ['x', 2, 'x', 0, 1])

       This op will only work on 3d tensors with no broadcastable
       dimensions.  The first dimension will be broadcastable,
       then we will have the third dimension of the input tensor as
       the second of the resulting tensor, etc. If the tensor has
       shape (20, 30, 40), the resulting tensor will have dimensions
       (1, 40, 1, 20, 30). (AxBxC tensor is mapped to 1xCx1xAxB tensor)

      DimShuffle((True, False), [1])

       This op will only work on 2d tensors with the first dimension broadcastable.
       The second dimension of the input tensor will be the first dimension of
       the resulting tensor. If the tensor has shape (1, 20), the resulting tensor
       will have shape (20, ).

    More examples:
      DimShuffle((), ['x']) -> make a 0d (scalar) into a 1d vector
      DimShuffle((False, False), [0, 1]) -> identity
      DimShuffle((False, False), [1, 0]) -> inverts the first and second dimensions
      DimShuffle((False,), ['x', 0]) -> make a row out of a 1d vector (N to 1xN)
      DimShuffle((False,), [0, 'x']) -> make a column out of a 1d vector (N to Nx1)
      DimShuffle((False, False, False), [2, 0, 1]) -> AxBxC to CxAxB
      DimShuffle((False, False), [0, 'x', 1]) -> AxB to Ax1xB
      DimShuffle((False, False), [1, 'x', 0]) -> AxB to Bx1xA

    The reordering of the dimensions can be done in numpy with the transpose function.
    Adding, subtracting dimensions can be done with reshape.
    """

    def __init__(self, input_broadcastable, new_order, inplace = False):
        """
        Usage: DimShuffle(input_broadcastable, new_order, inplace = False)

        - input_broadcastable: the expected broadcastable pattern of the
                               input
        - new_order: a list representing the relationship between the
                     input's dimensions and the output's dimensions. Each
                     element of the list can either be an index or 'x'.
        - inplace: if True, the output will be a view of the input.
                   If False, the output will be a copy of the input.

        If j = new_order[i] is an index, the output's ith dimension
          will be the input's jth dimension.
        If new_order[i] is 'x', the output's ith dimension will
          be 1 and Broadcast operations will be allowed to do broadcasting
          over that dimension.

        If input.broadcastable[i] == False then i must be found in new_order.
        Broadcastable dimensions, on the other hand, can be discarded.
        """
        input_broadcastable = tuple(input_broadcastable)
        self.input_broadcastable = input_broadcastable
        new_order = tuple(new_order)
        self.new_order = new_order
        self.inplace = inplace

        # list of dimensions of the input to drop
        self.drop = []
        i2j = {} # this maps i before dropping dimensions to j after dropping dimensions so self.shuffle can be set properly later on
        j = 0
        for i, b in enumerate(input_broadcastable):
            if i not in new_order:
                # we want to drop this dimension because it's not a value in new_order
                if b == 1: # 1 aka True
                    self.drop.append(i)
                else:
                    # we cannot drop non-broadcastable dimensions
                    raise ValueError("You cannot drop a non-broadcastable dimension.", (input_broadcastable, new_order))
            else:
                i2j[i] = j
                j += 1

        # transposition of non-broadcastable dimensions
        # This is how the dimensions will be permuted, without accounting for the extra
        # 'x' broadcastable dimensions to insert.
        self.shuffle = [i2j[x] for x in new_order if x != 'x']

        # list of dimensions of the output that are broadcastable and were not in the original input
        self.augment = [i for i, x in enumerate(new_order) if x == 'x']

        if self.inplace:
            self.view_map = {0: [0]}

        self._rehash()

    def __getstate__(self):
        d = dict(self.__dict__)
        del d['_hashval']
        return d
    def __setstate__(self, d):
        self.__dict__.update(d)
        self._rehash()

    def make_node(self, _input):
        input = as_tensor_variable(_input)
        ib = tuple(input.type.broadcastable)
        if not ib == self.input_broadcastable:
            raise TypeError("The number of dimensions and/or broadcastable pattern of the input is incorrect for this op. Expected %s, got %s." % (self.input_broadcastable, ib))
        ob = []
        for value in self.new_order:
            if value == 'x':
                ob.append(True)
            else:
                ob.append(ib[value])

        output = TensorType(dtype = input.type.dtype,
                        broadcastable = ob).make_variable()
        return Apply(self, [input], [output])

    def __eq__(self, other):
        # it's probably not necessary to compare input_broadcastable
        return type(self) == type(other) \
            and self.inplace == other.inplace \
            and self.new_order == other.new_order \
            and self.input_broadcastable == other.input_broadcastable

    def _rehash(self):
        self._hashval = hash(type(self).__name__) ^ hash(type(self).__module__) ^ hash(self.inplace) \
                ^ hash(self.new_order) ^ hash(self.input_broadcastable)

    def __hash__(self):
        return self._hashval

    def __str__(self):
        if self.inplace:
            return "InplaceDimShuffle{%s}" % ",".join(str(x) for x in self.new_order)
        else:
            return "DimShuffle{%s}" % ",".join(str(x) for x in self.new_order)

    def perform(self, node, (input, ), (storage, )):
        # drop
        res = input
        if type(res) != numpy.ndarray:
            raise TypeError(res)
        shape = list(res.shape)
        for drop in reversed(self.drop):
            shape.pop(drop)
        res = res.reshape(shape)

        # transpose
        res = res.transpose(self.shuffle)

        # augment
        shape = list(res.shape)
        for augm in self.augment:
            shape.insert(augm, 1)
        res = res.reshape(shape)

        # copy (if not inplace)
        if not self.inplace:
            res = numpy.copy(res)

        storage[0] = numpy.asarray(res) #asarray puts scalars back into array

    def c_code(self, node, name, (input,), (res,), sub):
        basename = input + '__view_or_copy'

        def statements(lst):
            return ';\n'.join(lst) + ';'

        nd_in = len(self.input_broadcastable)
        nd_out = len(self.new_order)

        check_input_nd = [('if (%(input)s->nd != ' + str(nd_in) + ')'
                '{PyErr_SetString(PyExc_NotImplementedError, "input nd"); %(fail)s;}')]

        clear_output = ['if (%(res)s) {Py_XDECREF(%(res)s);}']

        #get the copy / view of the input depending on whether we're doing things inplace or not.
        if self.inplace:
            get_base = ['{ PyArrayObject * %(basename)s = %(input)s', 'Py_INCREF((PyObject*)%(basename)s)']
        else:
            get_base = [('{ PyArrayObject * %(basename)s = (PyArrayObject*)PyArray_FromAny((PyObject*)%(input)s, NULL,'
                    '0, 0, NPY_ALIGNED|NPY_ENSURECOPY, NULL)')]

        shape_statements = ['npy_intp dimensions[%i]'%nd_out]
        for i, o in enumerate(self.new_order):
          if o != 'x':
            shape_statements += [('dimensions['+str(i)+'] = %(basename)s->dimensions['+str(o)+']')]
          else:
            shape_statements += [('dimensions['+str(i)+'] = 1')]
        #backport
        #shape_statements += [('dimensions['+str(i)+'] = %(basename)s->dimensions['+str(o)+']')
        #    if o != 'x' else
        #    ('dimensions['+str(i)+'] = 1')
        #    for i, o in enumerate(self.new_order)]


        strides_statements = ['npy_intp strides[%i]'%nd_out]

        #set the strides of the non-broadcasted dimensions
        for i, o in enumerate(self.new_order):
          if o != 'x':
             strides_statements += [('strides['+str(i)+'] = %(basename)s->strides['+str(o)+']')]
          else:
             strides_statements += [('strides['+str(i)+'] = 0')]
        #backport
        #strides_statements += [('strides['+str(i)+'] = %(basename)s->strides['+str(o)+']')
        #    if o != 'x' else
        #    ('strides['+str(i)+'] = 0')
        #    for i, o in enumerate(self.new_order)]

        # set the strides of the broadcasted dimensions
        # this algorithm is from numpy: PyArray_Newshape() in cvs/numpy/numpy/core/src/multiarraymodule.c
        strides_statements.append('if (strides['+str(nd_out)+'-1] == 0) strides['+str(nd_out)+'-1] = %(basename)s->descr->elsize')
        for i in xrange(nd_out-2,-1, -1):
            strides_statements.append("if (strides[%(i)s] == 0) strides[%(i)s] = strides[%(i)s+1] * dimensions[%(i)s+1]"%dict(i=str(i)))

        #
        # PyObject* PyArray_New(PyTypeObject* subtype, int nd, npy_intp* dims, int type_num,
        #                       npy_intp* strides, void* data, int itemsize, int flags, PyObject* obj)
        #
        close_bracket = [
                #create a new array, 
                ('%(res)s = (PyArrayObject*)PyArray_New(&PyArray_Type, '
                            '' + str(nd_out) + ', dimensions, '
                            'PyArray_TYPE(%(basename)s), strides, '
                            '%(basename)s->data, PyArray_ITEMSIZE(%(basename)s), '
                            #borrow only the writable flag from the base
                            # the NPY_OWNDATA flag will default to 0.
                            '(NPY_WRITEABLE*PyArray_ISWRITEABLE(%(basename)s)), NULL)'),
                #recalculate flags: CONTIGUOUS, FORTRAN, ALIGNED
                'PyArray_UpdateFlags(%(res)s, NPY_UPDATE_ALL)',
                #we are making a view in both inplace and non-inplace cases
                '%(res)s->base = (PyObject*)%(basename)s', 
                '}']

        full_code = statements(check_input_nd 
                + clear_output
                + get_base
                + shape_statements 
                + strides_statements
                + close_bracket)

        if 0:
            print 'C_CODE'
            print ''
            print self
            print "IN BROAD", self.input_broadcastable
            print "NEW ORDER", self.new_order
            print "SHUFFLE", self.shuffle
            print "AUGMENT", self.augment
            print '------------'
            print ''
            print full_code

            if 0:
                import sys
                sys.exit()

        return full_code % dict(locals(), **sub)

    def grad(self, (x, ), (gz, )):
        gz = as_tensor_variable(gz)
        grad_order = ['x'] * len(x.type.broadcastable)
        for i, v in enumerate(self.new_order):
            if v != 'x':
                grad_order[v] = i
        return [DimShuffle(gz.type.broadcastable, grad_order, inplace=True)(Elemwise(scalar.identity)(gz))]



class DimShufflePrinter:

    def __p(self, new_order, pstate, r):
        if new_order != () and  new_order[0] == 'x':
            return "%s" % self.__p(new_order[1:], pstate, r)
#            return "[%s]" % self.__p(new_order[1:], pstate, r)
        if list(new_order) == range(r.type.ndim):
            return pstate.pprinter.process(r)
        if list(new_order) == list(reversed(range(r.type.ndim))):
            return "%s.T" % pstate.pprinter.process(r)
        return "DimShuffle{%s}(%s)" % (", ".join(map(str, new_order)), pstate.pprinter.process(r))

    def process(self, r, pstate):
        if r.owner is None:
            raise TypeError("Can only print DimShuffle.")
        elif isinstance(r.owner.op, DimShuffle):
            ord = r.owner.op.new_order
            return self.__p(ord, pstate, r.owner.inputs[0])            
        else:
            raise TypeError("Can only print DimShuffle.")

pprint.assign(lambda pstate, r: r.owner and isinstance(r.owner.op, DimShuffle), DimShufflePrinter())


################
### Elemwise ###
################

class Elemwise(Op):
    """
    Generalizes a scalar op to tensors.

    All the inputs must have the same number of dimensions. When the
    Op is performed, for each dimension, each input's size for that
    dimension must be the same. As a special case, it can also be 1
    but only if the input's broadcastable flag is True for that
    dimension. In that case, the tensor is (virtually) replicated
    along that dimension to match the size of the others.

    The dtypes of the outputs mirror those of the scalar Op that is
    being generalized to tensors. In particular, if the calculations
    for an output are done inplace on an input, the output type must
    be the same as the corresponding input type (see the doc of
    scalar.ScalarOp to get help about controlling the output type)

    Examples:
      Elemwise(add) # represents + on tensors (x + y)
      Elemwise(add, {0 : 0}) # represents the += operation (x += y)
      Elemwise(add, {0 : 1}) # represents += on the second argument (y += x)
      Elemwise(mul)(rand(10, 5), rand(1, 5)) # the second input is completed along the first dimension to match the first input
      Elemwise(true_div)(rand(10, 5), rand(10, 1)) # same but along the second dimension
      Elemwise(int_div)(rand(1, 5), rand(10, 1)) # the output has size (10, 5)
      Elemwise(log)(rand(3, 4, 5))
    """

    def __init__(self, scalar_op, inplace_pattern = {}, name = None):
        """
        Usage: Elemwise(scalar_op, inplace_pattern = {})

        * scalar_op: an instance of a subclass of scalar.ScalarOp which works uniquely on
                     scalars
        * inplace_pattern: a dictionary that maps the index of an output to the
                           index of an input so the output is calculated inplace using
                           the input's storage. (Just like destroymap, but without the lists.)
        """
        self.name = name
        self.scalar_op = scalar_op
        self.inplace_pattern = inplace_pattern
        self.destroy_map = dict((o, [i]) for o, i in inplace_pattern.items())
        if scalar_op.nin > 0:
            self.ufunc = numpy.frompyfunc(scalar_op.impl, scalar_op.nin, scalar_op.nout)
        else:
            self.ufunc = None

        #precompute the hash of this node
        self._rehash()

    def __getstate__(self):
        d = copy(self.__dict__)
        d.pop('ufunc')
        d.pop('__epydoc_asRoutine', None)
        d.pop('_hashval')
        return d
    
    def __setstate__(self, d):
        self.__dict__.update(d)
        if self.scalar_op.nin > 0:
            self.ufunc = numpy.frompyfunc(self.scalar_op.impl, self.scalar_op.nin, self.scalar_op.nout)
        else:
            self.ufunc = None
        self._rehash()

    def make_node(self, *inputs):
        """
        If the inputs have different number of dimensions, their shape
        is left-completed to the greatest number of dimensions with 1s
        using DimShuffle.
        """

        inputs = map(as_tensor_variable, inputs)
        shadow = self.scalar_op.make_node(*[Scalar(dtype = t.type.dtype)() for t in inputs])

        target_length = max([input.type.ndim for input in inputs])

        args = []
        for input in inputs:
            length = input.type.ndim
            difference = target_length - length
            if not difference:
                args.append(input)
            else:
                # TODO: use LComplete instead
                args.append(DimShuffle(
                    input.type.broadcastable, 
                    ['x']*difference + range(length),
                    inplace = True)(input))
        inputs = args

        #HERE: all the broadcast dims have the same length now

        #cleverness: we iterate over the first, second, third broadcast flag of all inputs in
        #parallel... the all() gives us each output broadcastable bit in turn.

        #it is multiplied by nout because Elemwise supports multiple outputs (nout of them)
        out_broadcastables = [[all(bcast) for bcast in zip(*[input.type.broadcastable for input in inputs])]] * shadow.nout

        #inplace_pattern maps output idx -> input idx
        inplace_pattern = self.inplace_pattern
        if inplace_pattern:
            for overwriter, overwritten in inplace_pattern.items():
                for ob, ib in zip(out_broadcastables[overwriter], inputs[overwritten].type.broadcastable):
                    if ib and not ob:
                        raise ValueError("Operation cannot be done inplace on an input with broadcasted dimensions.")
        out_dtypes = [o.type.dtype for o in shadow.outputs]
        if any(inputs[i].type.dtype != out_dtypes[o] for o, i in inplace_pattern.items()):
            raise TypeError("Cannot do an inplace operation on incompatible data types.", 
                    ([i.type.dtype for i in inputs], out_dtypes, inplace_pattern))
        outputs = [TensorType(dtype = dtype, broadcastable = broadcastable)() for dtype, broadcastable in zip(out_dtypes, out_broadcastables)]
        return Apply(self, inputs, outputs)

    def __eq__(self, other):
        if type(self) == type(other):
            items = self.inplace_pattern.items()
            other_items = other.inplace_pattern.items()
            items.sort()
            other_items.sort()
            rval = (self.scalar_op == other.scalar_op) and (items == other_items)
            return rval
        return False

    def _rehash(self):
        items = self.inplace_pattern.items()
        items.sort()
        first_part = [k for k,v in items]
        second_part = []
        for k,v in items:
          if isinstance(v, (tuple, list)):
            second_part += [tuple(v)]
          else:
            second_part += [v]
        tuple_items = tuple(first_part + second_part)
        #backport
        #tuple_items = tuple([k for k,v in items] + [(tuple(v) if isinstance(v, (tuple, list)) else v) for k,v in items])
        h = hash('Elemwise') ^ hash(self.scalar_op) ^ hash(tuple_items)
        assert h == getattr(self,'_hashval', h)
        self._hashval = h

    def __hash__(self):
        return self._hashval

    def __str__(self):
        if self.name is None:
            if self.inplace_pattern:
                items = self.inplace_pattern.items()
                items.sort()
                return "Elemwise{%s}%s" % (self.scalar_op, str(items))
            else:
                return "Elemwise{%s}" % (self.scalar_op)
        else:
            return self.name

    def grad(self, inputs, ograds):
        ograds = map(as_tensor_variable, ograds) # this shouldn't be necessary...
        scalar_inputs = [Scalar(dtype = t.type.dtype)() for t in inputs]
        scalar_ograds = [Scalar(dtype = ograd.type.dtype)() for ograd in ograds]
        scalar_igrads = self.scalar_op.grad(scalar_inputs, scalar_ograds)
        nd = len(inputs[0].type.broadcastable) # this is the same for everyone
        def transform(r):
            # From a graph of ScalarOps, make a graph of Broadcast ops.
            if r in scalar_inputs:
                return inputs[scalar_inputs.index(r)]
            if r in scalar_ograds:
                return ograds[scalar_ograds.index(r)]
            node = r.owner
            if node is None:
                # the gradient contains a constant, translate it as
                # an equivalent TensorType of size 1 and proper number of dimensions
                res = TensorConstant(TensorType(dtype = r.type.dtype,
                                            broadcastable = ()),
                                     numpy.asarray(r.data)) # .reshape(b)
                return DimShuffle((), ['x']*nd, inplace = True)(res)
            new_r = Elemwise(node.op, {})(*[transform(input) for input in node.inputs])
            return new_r
        ret = []
        for scalar_igrad, input in zip(scalar_igrads, inputs):
            if scalar_igrad is None:
                # undefined gradient
                ret.append(None)
                continue
            r = transform(scalar_igrad)

            # list of all the dimensions that are broadcastable for that input so we
            # can sum over them
            # todo: only count dimensions that were effectively broadcasted
            to_sum = [i for i, bcast in enumerate(input.type.broadcastable) if bcast]

            if to_sum:
                shuffle = []
                j = 0
                for bcast in input.type.broadcastable:
                    if bcast == 1:
                        shuffle.append('x')
                    else:
                        shuffle.append(j)
                        j += 1
                sr = Sum(axis = to_sum)(r)
                sr = DimShuffle(sr.type.broadcastable, shuffle)(sr)
                ret.append(sr)
            else:
                ret.append(r)
        return ret

    def perform(self, node, inputs, output_storage):
        maxsize = max(len(input.shape) for input in inputs)
        for dims in zip(*[[(1, True)]*(maxsize - len(input.shape)) + zip(input.shape, sinput.type.broadcastable)
                          for input, sinput in zip(inputs, node.inputs)]):
            if max(d for d,b in dims) != 1 and (1, False) in dims:
                # yes there may be more compact ways to write this code,
                # but please maintain python 2.4 compatibility (no "x if c else y")
                msg = []
                assert len(inputs) == len(node.inputs)
                for input, sinput in zip(inputs, node.inputs):
                    assert len(input.shape) == len(sinput.type.broadcastable)
                    msg2 = []
                    for d, b in zip(input.shape, sinput.type.broadcastable):
                        if b:
                            msg2 += ['*'] 
                        else:
                            msg2 += [str(d)]
                    msg.append('(%s)' % ", ".join(msg2))

                raise ValueError('Dimension mismatch; shapes are %s' %
                        ', '.join(msg))
                                 #', '.join('(%s)' % ', '.join(msg)))
                #backport
                #raise ValueError('Dimension mismatch; shapes are %s' %
                #                 ', '.join('(%s)' % ', '.join('*' if b else str(d)
                #                                              for d, b in zip(input.shape, sinput.type.broadcastable))
                #                           for input, sinput in zip(inputs, node.inputs)))
                # Other mismatches will be caught by the ufunc
        if not self.inplace_pattern:
            for output, storage in zip(node.outputs, output_storage):
                odat = storage[0]
                shape = [max(values) for values in zip(*[input.shape for input in inputs])]
                if odat is not None:
                    # reuse storage if we can
                    odat.resize(shape, refcheck = 0)
                else:
                    odat = numpy.ndarray(shape, dtype = output.type.dtype)
                storage[0] = odat
        else:
            for i, (output, storage) in enumerate(zip(node.outputs, output_storage)):
                #i is an output idx
                if i in self.inplace_pattern:
                    odat = inputs[self.inplace_pattern[i]]
                else:
                    odat = storage[0]
                    shape = [max(values) for values in zip(*[input.shape for input in inputs])]
                    if odat is not None:
                        odat.resize(shape, refcheck = 0)
                    else:
                        odat = numpy.ndarray(shape, dtype = output.type.dtype)
                storage[0] = odat
        # the second calling form is used because in certain versions of numpy
        # the first (faster) version leads to segfaults
        ufunc_args = inputs # + output_storage
        ufunc = self.ufunc or numpy.frompyfunc(self.scalar_op.impl, len(inputs), self.scalar_op.nout)
        
        try:
            variables = ufunc(*ufunc_args)
        except Exception, e:
            errormsg = 'Failed calling ufunc for op', self.scalar_op,\
                        'for params of shape', [arg.shape for arg in ufunc_args]
            e.args = e.args + errormsg
            raise e
        if ufunc.nout == 1: variables = [variables]
        for variable, storage in zip(variables, output_storage):
            if storage[0].shape:
                storage[0][:] = variable
            else:
                storage[0].itemset(variable)
        # the following should be used instead of the previous loop, unfortunately it tends to segfault
        # self.ufunc(*(ufunc_args+[s[0] for s in output_storage]))

    def _c_all(self, node, name, inames, onames, sub):
        _inames = inames
        _onames = onames

        inames = gof.utils.uniq(inames)
        inputs = gof.utils.uniq(node.inputs)

        defines = ""
        undefs = ""
        dmap = dict([(node.outputs[o], [node.inputs[i]]) for o, i in self.inplace_pattern.items()])

        idtypes = [input.type.dtype_specs()[1] for input in inputs]

        real = zip(*[(r, s, r.type.dtype_specs()[1])
                     for r, s in zip(node.outputs, onames) if r not in dmap])
        if real:
            real_outputs, real_onames, real_odtypes = real
        else:
            real_outputs, real_onames, real_odtypes = [], [], []

        aliased = zip(*[(r, s)
                        for (r, s) in zip(node.outputs, onames) if r in dmap])
        if aliased:
            aliased_outputs, aliased_onames = aliased
        else:
            aliased_outputs, aliased_onames = [], []

        orders = [[x and 'x' or i for i, x in enumerate(input.type.broadcastable)] for input in inputs]
        nnested = len(orders[0])
        sub = dict(sub)
        for i, (input, iname) in enumerate(zip(inputs, inames)):
            sub['lv%i' % i] = iname
        decl = cgen.make_declare(orders, idtypes, sub)
        checks = cgen.make_checks(orders, idtypes, sub)

        alloc = ""
        for output, oname, odtype in zip(real_outputs, real_onames, real_odtypes):
            i += 1
            sub['lv%i' % i] = oname
            sub['olv'] = oname
            alloc += cgen.make_declare([range(nnested)], [odtype], dict(sub, lv0 = oname))
            alloc += cgen.make_alloc(orders, odtype, sub)
            alloc += cgen.make_checks([range(nnested)], [odtype], dict(sub, lv0 = oname))

        for output, oname in zip(aliased_outputs, aliased_onames):
            iname = inames[inputs.index(dmap[output][0])]
            alloc += """
            if (%(oname)s) {
                Py_XDECREF(%(oname)s);
            }
            %(oname)s = %(iname)s;
            Py_XINCREF(%(oname)s);
            """ % locals()
            defines += "#define %(oname)s_i %(iname)s_i" % locals()
            undefs += "#undef %(oname)s_i" % locals()

        task_code = self.scalar_op.c_code(Apply(self.scalar_op,
                                                [Scalar(dtype = input.type.dtype)() for input in node.inputs],
                                                [Scalar(dtype = output.type.dtype)() for output in node.outputs]),
                                          name + '_scalar_',
                                          ["%s_i" % s for s in _inames],
                                          ["%s_i" % s for s in onames],
                                          sub)
        task_decl = "".join(["%(dtype)s& %(name)s_i = *%(name)s_iter;\n" % locals() for name, dtype in zip(inames + list(real_onames), idtypes + list(real_odtypes))])
        code = """
        {
            %(defines)s
            %(task_decl)s
            %(task_code)s
            %(undefs)s
        }
        """ % locals()
        if nnested:
            all_code = [("", "")] * (nnested - 1) + [("", code)] + [""]
        else:
            all_code = [code]
        loop = cgen.make_loop(orders + [range(nnested)] * len(real_onames), idtypes + list(real_odtypes), all_code, sub)
        return decl, checks, alloc, loop

    def c_code(self, node, name, inames, onames, sub):
        code = "\n".join(self._c_all(node, name, inames, onames, sub))
        return code

    def c_support_code(self):
        return self.scalar_op.c_support_code()

    def c_code_cache_version(self):
        return (4,)

# def elemwise_to_scal(env):
#     mapping = {}
#     inputs = []
#     outputs = []
#     for node in env.io_toposort():
#         if not isinstance(node.op, Elemwise):
#             raise TypeError('All ops in the graph must be Elemwise.')



################
### CAReduce ###
################

class CAReduce(Op):
    """
    Reduces a scalar operation along the specified axis(es).

    The output will have the same shape as the input minus the reduced
    dimensions. It will contain the variable of accumulating all values
    over the reduced dimensions using the specified scalar op.

    Examples:
     CAReduce(add) -> sum
     CAReduce(mul) -> product
     CAReduce(_or) -> any # not lazy
     CAReduce(_and) -> all # not lazy

    In order to (eventually) optimize memory usage patterns,
    L{CAReduce} makes zero guarantees on the order in which it
    iterates over the dimensions and the elements of the
    array(s). Therefore, to ensure consistent variables, the scalar
    operation represented by the reduction must be both commutative
    and associative (eg add, multiply, binary or/and/xor - but not
    subtract, divide or power).
    """

    def __init__(self, scalar_op, axis = None):
        """
        Usage: CAReduce(scalar_op, axis = None)

        * scalar_op: a binary scalar op with only one output.
                     It must be commutative and associative.
        * axis: - the dimension along which we want to reduce
                - list of dimensions that we want to reduce
                - if None, all dimensions are reduced
        """
        if scalar_op.nin not in [-1, 2] or scalar_op.nout != 1:
            raise NotImplementedError("CAReduce only supports binary functions with a single output.")
        self.scalar_op = scalar_op
        if isinstance(axis, int):
            self.axis = [axis]
        else:
            self.axis = axis
        self.ufunc = numpy.frompyfunc(scalar_op.impl, 2, 1)

        # CAReduce output views input when reducing scalars
        # See ticket #417: http://www.pylearn.org/theano/trac/ticket/417
        self.view_map = {0: [0]}

    def _output_dtype(self, input_dtype):
        return input_dtype

    def make_node(self, input):
        if self.axis is not None:
            for axis in self.axis:
                if axis >= input.type.ndim:
                    raise TypeError('Not enough dimensions on %s to reduce on axis %s' % (input, axis))
        input = as_tensor_variable(input)
        axis = self.axis
        if axis is None:
            axis = range(len(input.type.broadcastable))
        output = TensorType(dtype = self._output_dtype(input.type.dtype),
                             broadcastable = [x for i, x in enumerate(input.type.broadcastable) if i not in axis])()
        return Apply(self, [input], [output])

    def __getstate__(self):
        d = copy(self.__dict__)
        d.pop('ufunc')
        return d
    
    def __setstate__(self, d):
        self.__dict__.update(d)
        self.ufunc = numpy.frompyfunc(self.scalar_op.impl, 2, 1)

    def __eq__(self, other):
        return type(self) == type(other) and self.scalar_op == other.scalar_op and self.axis == other.axis

    def __hash__(self):
        if self.axis is None:
            return hash(self.scalar_op)
        else:
            return hash(self.scalar_op) ^ hash(tuple(self.axis))

    def __str__(self):
        if self.axis is not None:
            return "Reduce{%s}{%s}" % (self.scalar_op, ", ".join(str(x) for x in self.axis))
        else:
            return "Reduce{%s}" % self.scalar_op

    def perform(self, node, (input, ), (output, )):
        axis = self.axis
        if axis is None:
            axis = range(input.ndim)
        variable = input
        to_reduce = reversed(sorted(axis))
        if to_reduce:
            for dimension in to_reduce:
                variable = self.ufunc.reduce(variable, dimension)
            output[0] = numpy.asarray(variable, dtype = node.outputs[0].type.dtype)
        else:
            output[0] = numpy.copy(variable)

    def _c_all(self, node, name, inames, onames, sub):

        input = node.inputs[0]
        output = node.outputs[0]

        iname = inames[0]
        oname = onames[0]

        idtype = input.type.dtype_specs()[1]
        odtype = output.type.dtype_specs()[1]

        axis = self.axis
        if axis is None:
            axis = range(len(input.type.broadcastable))

        if axis == ():
            op = Elemwise(scalar.identity)
            return op._c_all(op.make_node(input), name, inames, onames, sub)

        order1 = [i for i in xrange(input.type.ndim) if i not in axis]
        order = order1 + list(axis)

        nnested = len(order1)

        sub = dict(sub)
        for i, (input, iname) in enumerate(zip(node.inputs, inames)):
            sub['lv%i' % i] = iname

        decl = cgen.make_declare([order], [idtype], sub)
        checks = cgen.make_checks([order], [idtype], sub)

        alloc = ""
        i += 1
        sub['lv%i' % i] = oname
        sub['olv'] = oname
        alloc += cgen.make_declare([range(nnested) + ['x'] * len(axis)], [odtype], dict(sub, lv0 = oname))
        alloc += cgen.make_alloc([order1], odtype, sub)
        alloc += cgen.make_checks([range(nnested) + ['x'] * len(axis)], [odtype], dict(sub, lv0 = oname))

        task0_decl = "%(dtype)s& %(name)s_i = *%(name)s_iter;\n%(name)s_i = %(identity)s;" % dict(dtype = odtype,
                                                                                                  name = onames[0],
                                                                                                  identity = self.scalar_op.identity)

        task1_decl = "%(dtype)s& %(name)s_i = *%(name)s_iter;\n" % dict(dtype = idtype, name = inames[0])

        task1_code = self.scalar_op.c_code(Apply(self.scalar_op,
                                                 [Scalar(dtype = input.type.dtype)() for input in node.inputs*2],
                                                 [Scalar(dtype = output.type.dtype)() for input in node.outputs]),
                                           None,
                                           ["%s_i" % onames[0], "%s_i" % inames[0]],
                                           ["%s_i" % onames[0]],
                                           sub)
        code1 = """
        {
            %(task1_decl)s
            %(task1_code)s
        }
        """ % locals()

        if node.inputs[0].type.ndim:
            if len(axis) == 1:
                all_code = [("", "")] * nnested + [(task0_decl, code1), ""]
            else:
                all_code = [("", "")] * nnested + [(task0_decl, "")] + [("", "")] * (len(axis) - 2) + [("", code1), ""]
        else:
            all_code = [task0_decl + code1]
        loop = cgen.make_loop([order, range(nnested) + ['x'] * len(axis)], [idtype, odtype], all_code, sub)
        return decl, checks, alloc, loop

    def c_code(self, node, name, inames, onames, sub):
        code = "\n".join(self._c_all(node, name, inames, onames, sub))
        return code


class Sum(CAReduce):
    """
    Sums all the values of a tensor along the specified axis(es).

    Equivalent to CAReduce(scalar.add, axis = axis), with the
    difference that this defines the gradient of sum wrt its tensor
    input.
    """
    def __init__(self, axis = None):
        CAReduce.__init__(self, scalar.add, axis)

    def _output_dtype(self, idtype):
        # we want to protect against overflow
        return dict(
                int8='int32',
                int16='int32',
                int32='int64',
                ).get(idtype, idtype)

    def grad(self, (x, ), (gz, )):
        gz = as_tensor_variable(gz)
        axis = self.axis
        if axis is None:
            axis = range(x.type.ndim)
        if axis == ():
            return gz,
        new_dims = []
        i = 0
        for j, _ in enumerate(x.type.broadcastable):
            if j in axis:
                new_dims.append('x')
            else:
                new_dims.append(i)
                i += 1
        return Elemwise(scalar.second)(x, DimShuffle(gz.type.broadcastable, new_dims)(gz)),

    def __str__(self):
        if self.axis is None:
            return "Sum"
        else:
            return "Sum{%s}" % ", ".join(map(str, self.axis))


