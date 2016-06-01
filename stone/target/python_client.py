from __future__ import absolute_import, division, print_function, unicode_literals

import argparse
import re

from stone.data_type import (
    is_nullable_type,
    is_struct_type,
    is_tag_ref,
    is_union_type,
    is_user_defined_type,
    is_void_type,
)
from stone.generator import CodeGenerator
from stone.target.python_helpers import (
    fmt_class,
    fmt_func,
    fmt_obj,
    fmt_type,
    fmt_var,
)
from stone.target.python_types import (
    class_name_for_data_type,
)

# This will be at the top of the generated file.
base = """\
# Auto-generated by Stone, do not modify.

from abc import ABCMeta, abstractmethod
"""

# Matches format of Babel doc tags
doc_sub_tag_re = re.compile(':(?P<tag>[A-z]*):`(?P<val>.*?)`')

DOCSTRING_CLOSE_RESPONSE = """\
If you do not consume the entire response body, then you must call close on the
response object, otherwise you will max out your available connections. We
recommend using the `contextlib.closing
<https://docs.python.org/2/library/contextlib.html#contextlib.closing>`_
context manager to ensure this."""

_cmdline_parser = argparse.ArgumentParser(
    prog='python-client-generator',
    description=(
        'Generates a Python class with a method for each route. Extend the '
        'generated class and implement the abstract request() method. This '
        'class assumes that the python_types generator was used with the same '
        'output directory.'),
)
_cmdline_parser.add_argument(
    '-m',
    '--module-name',
    required=True,
    type=str,
    help=('The name of the Python module to generate. Please exclude the .py '
          'file extension.'),
)
_cmdline_parser.add_argument(
    '-c',
    '--class-name',
    required=True,
    type=str,
    help='The name of the Python class that contains each route as a method.',
)


class PythonClientGenerator(CodeGenerator):

    cmdline_parser = _cmdline_parser

    def generate(self, api):
        """Generates a module called "base".

        The module will contain a "DropboxBase" class that will have a method
        for each route across all namespaces.
        """
        with self.output_to_relative_path('%s.py' % self.args.module_name):
            self.emit_raw(base)
            # Import "warnings" if any of the routes are deprecated.
            found_deprecated = False
            for namespace in api.namespaces.values():
                for route in namespace.routes:
                    if route.deprecated:
                        self.emit('import warnings')
                        found_deprecated = True
                        break
                if found_deprecated:
                    break
            self.emit()
            self._generate_imports(api.namespaces.values())
            self.emit()
            self.emit()  # PEP-8 expects two-blank lines before class def
            self.emit('class %s(object):' % self.args.class_name)
            with self.indent():
                self.emit('__metaclass__ = ABCMeta')
                self.emit()
                self.emit('@abstractmethod')
                self.emit(
                    'def request(self, route, namespace, arg, arg_binary=None):')
                with self.indent():
                    self.emit('pass')
                self.emit()
                self._generate_route_methods(api.namespaces.values())

    def _generate_imports(self, namespaces):
        # Only import namespaces that have user-defined types defined.
        ns_names_to_import = [ns.name for ns in namespaces if ns.data_types]
        self.emit('from . import (')
        with self.indent():
            for ns in ns_names_to_import:
                self.emit(ns + ',')
        self.emit(')')

    def _generate_route_methods(self, namespaces):
        """Creates methods for the routes in each namespace. All data types
        and routes are represented as Python classes."""
        for namespace in namespaces:
            if namespace.routes:
                self.emit('# ------------------------------------------')
                self.emit('# Routes in {} namespace'.format(namespace.name))
                self.emit()
                for route in namespace.routes:
                    self._generate_route(namespace, route)

    def _generate_route(self, namespace, route):
        """Generates Python methods that correspond to a route."""
        self._generate_route_helper(namespace, route)
        if route.attrs.get('style') == 'download':
            self._generate_route_helper(namespace, route, True)

    def _generate_route_helper(self, namespace, route, download_to_file=False):
        """Generate a Python method that corresponds to a route.

        :param namespace: Namespace that the route belongs to.
        :param bool download_to_file: Whether a special version of the route
            that downloads the response body to a file should be generated.
            This can only be used for download-style routes.
        """
        arg_data_type = route.arg_data_type
        result_data_type = route.result_data_type

        request_binary_body = route.attrs.get('style') == 'upload'
        response_binary_body = route.attrs.get('style') == 'download'

        if download_to_file:
            assert response_binary_body, 'download_to_file can only be set ' \
                'for download-style routes.'
            self._generate_route_method_decl(namespace,
                                             route,
                                             arg_data_type,
                                             request_binary_body,
                                             method_name_suffix='_to_file',
                                             extra_args=['download_path'])
        else:
            self._generate_route_method_decl(namespace,
                                             route,
                                             arg_data_type,
                                             request_binary_body)

        with self.indent():
            extra_request_args = None
            extra_return_arg = None
            footer=None
            if request_binary_body:
                extra_request_args = [('f',
                                       None,
                                       'A string or file-like obj of data.')]
            elif download_to_file:
                extra_request_args = [('download_path',
                                       'str',
                                       'Path on local machine to save file.')]
            if response_binary_body:
                extra_return_arg = ':class:`requests.models.Response`'
                if not download_to_file:
                    footer = DOCSTRING_CLOSE_RESPONSE

            self._generate_docstring_for_func(
                namespace,
                arg_data_type,
                result_data_type,
                route.error_data_type,
                overview=self.process_doc(route.doc, self._docf),
                extra_request_args=extra_request_args,
                extra_return_arg=extra_return_arg,
                footer=footer,
            )

            self._maybe_generate_deprecation_warning(route)

            # Code to instantiate a class for the request data type
            if is_void_type(arg_data_type):
                self.emit('arg = None')
            elif is_struct_type(arg_data_type):
                self.generate_multiline_list(
                    [f.name for f in arg_data_type.all_fields],
                    before='arg = {}.{}'.format(
                        arg_data_type.namespace.name,
                        fmt_class(arg_data_type.name)),
                    )
            elif not is_union_type(arg_data_type):
                raise AssertionError('Unhandled request type %r' %
                                     arg_data_type)

            # Code to make the request
            args = [
                '{}.{}'.format(namespace.name, fmt_var(route.name)),
                "'{}'".format(namespace.name),
                'arg']
            if request_binary_body:
                args.append('f')
            else:
                args.append('None')
            self.generate_multiline_list(args, 'r = self.request', compact=False)

            if download_to_file:
                self.emit('self._save_body_to_file(download_path, r[1])')
                if is_void_type(result_data_type):
                    self.emit('return None')
                else:
                    self.emit('return r[0]')
            else:
                if is_void_type(result_data_type):
                    self.emit('return None')
                else:
                    self.emit('return r')
        self.emit()

    def _generate_route_method_decl(
            self, namespace, route, arg_data_type, request_binary_body,
            method_name_suffix=None, extra_args=None):
        """Generates the method prototype for a route."""
        method_name = fmt_func(route.name)
        namespace_name = fmt_func(namespace.name)
        if method_name_suffix:
            method_name += method_name_suffix
        args = ['self']
        if extra_args:
            args += extra_args
        if request_binary_body:
            args.append('f')
        if is_struct_type(arg_data_type):
            for field in arg_data_type.all_fields:
                if is_nullable_type(field.data_type):
                    args.append('{}=None'.format(field.name))
                elif field.has_default:
                    # TODO(kelkabany): Decide whether we really want to set the
                    # default in the argument list. This will send the default
                    # over the wire even if it isn't overridden. The benefit is
                    # it locks in a default even if it is changed server-side.
                    if is_user_defined_type(field.data_type):
                        ns = field.data_type.namespace
                    else:
                        ns = None
                    arg = '{}={}'.format(
                        field.name,
                        self._generate_python_value(ns, field.default))
                    args.append(arg)
                else:
                    args.append(field.name)
        elif is_union_type(arg_data_type):
            args.append('arg')
        elif not is_void_type(arg_data_type):
            raise AssertionError('Unhandled request type: %r' %
                                 arg_data_type)
        self.generate_multiline_list(
            args, 'def {}_{}'.format(namespace_name, method_name), ':')

    def _maybe_generate_deprecation_warning(self, route):
        if route.deprecated:
            msg = '{} is deprecated.'.format(route.name)
            if route.deprecated.by:
                msg += ' Use {}.'.format(route.deprecated.by.name)
            args = ["'{}'".format(msg), 'DeprecationWarning']
            self.generate_multiline_list(args, before='warnings.warn', delim=('(', ')'), compact=False)

    def _generate_docstring_for_func(self, namespace, arg_data_type,
                                     result_data_type=None, error_data_type=None,
                                     overview=None, extra_request_args=None,
                                     extra_return_arg=None, footer=None):
        """
        Generates a docstring for a function or method.

        This function is versatile. It will create a docstring using all the
        data that is provided.

        :param arg_data_type: The data type describing the argument to the
            route. The data type should be a struct, and each field will be
            treated as an input parameter of the method.
        :param result_data_type: The data type of the route result.
        :param error_data_type: The data type of the route result in the case
            of an error.
        :param str overview: A description of the route that will be located
            at the top of the docstring.
        :param extra_request_args: [(field name, field type, field doc), ...]
            Describes any additional parameters for the method that aren't a
            field in arg_data_type.
        :param str extra_return_arg: Name of an additional return type that. If
            this is specified, it is assumed that the return of the function
            will be a tuple of return_data_type and extra_return-arg.
        :param str footer: Additional notes at the end of the docstring.
        """
        fields = [] if is_void_type(arg_data_type) else arg_data_type.fields
        if not fields and not overview:
            # If we don't have an overview or any input parameters, we skip the
            # docstring altogether.
            return

        self.emit('"""')
        if overview:
            self.emit_wrapped_text(overview)

        # Description of all input parameters
        if extra_request_args or fields:
            if overview:
                # Add a blank line if we had an overview
                self.emit()

            if extra_request_args:
                for name, data_type_name, doc in extra_request_args:
                    if data_type_name:
                        field_doc = ':param {} {}: {}'.format(data_type_name,
                                                              name, doc)
                        self.emit_wrapped_text(field_doc,
                                               subsequent_prefix='    ')
                    else:
                        self.emit_wrapped_text(
                            ':param {}: {}'.format(name, doc),
                            subsequent_prefix='    ')

            if is_struct_type(arg_data_type):
                for field in fields:
                    if field.doc:
                        if is_user_defined_type(field.data_type):
                            field_doc = ':param {}: {}'.format(
                                field.name, self.process_doc(field.doc, self._docf))
                        else:
                            field_doc = ':param {} {}: {}'.format(
                                self._format_type_in_doc(namespace, field.data_type),
                                field.name,
                                self.process_doc(field.doc, self._docf),
                            )
                        self.emit_wrapped_text(
                            field_doc, subsequent_prefix='    ')
                        if is_user_defined_type(field.data_type):
                            # It's clearer to declare the type of a composite on
                            # a separate line since it references a class in
                            # the dropbox package.
                            self.emit(':type {}: {}'.format(
                                field.name,
                                self._format_type_in_doc(namespace, field.data_type),
                            ))
                    else:
                        # If the field has no docstring, then just document its
                        # type.
                        field_doc = ':type {}: {}'.format(
                            field.name,
                            self._format_type_in_doc(namespace, field.data_type),
                        )
                        self.emit_wrapped_text(field_doc)

            elif is_union_type(arg_data_type):
                if arg_data_type.doc:
                    self.emit_wrapped_text(':param arg: {}'.format(
                        self.process_doc(arg_data_type.doc, self._docf)),
                        subsequent_prefix='    ')
                self.emit(':type arg: {}'.format(
                    self._format_type_in_doc(namespace, arg_data_type)))

        if overview and not (extra_request_args or fields):
            # Only output an empty line if we had an overview and haven't
            # started a section on declaring types.
            self.emit()

        if extra_return_arg:
            # Special case where the function returns a tuple. The first
            # element is the JSON response. The second element is the
            # the extra_return_arg param.
            args = []
            if is_void_type(result_data_type):
                args.append('None')
            else:
                rtype = self._format_type_in_doc(namespace,
                                                 result_data_type)
                args.append(rtype)
            args.append(extra_return_arg)
            self.generate_multiline_list(args, ':rtype: ')
        else:
            if is_void_type(result_data_type):
                self.emit(':rtype: None')
            else:
                rtype = self._format_type_in_doc(namespace, result_data_type)
                self.emit(':rtype: {}'.format(rtype))

        if not is_void_type(error_data_type) and error_data_type.fields:
            self.emit(':raises: :class:`dropbox.exceptions.ApiError`')
            self.emit()
            # To provide more clarity to a dev who reads the docstring, state
            # the error class that will be returned in the reason field of an
            # ApiError object.
            self.emit('If this raises, ApiError.reason is of type:')
            with self.indent():
                self.emit(self._format_type_in_doc(namespace, error_data_type))

        if footer:
            self.emit()
            self.emit_wrapped_text(footer)
        self.emit('"""')

    def _docf(self, tag, val):
        """
        Callback used as the handler argument to process_docs(). This converts
        Babel doc references to Sphinx-friendly annotations.
        """
        if tag == 'type':
            return ':class:`{}`'.format(val)
        elif tag == 'route':
            return ':meth:`{}`'.format(fmt_func(val))
        elif tag == 'link':
            anchor, link = val.rsplit(' ', 1)
            return '`{} <{}>`_'.format(anchor, link)
        elif tag == 'val':
            if val == 'null':
                return 'None'
            elif val == 'true' or val == 'false':
                return '``{}``'.format(val.capitalize())
            else:
                return val
        elif tag == 'field':
            return '``{}``'.format(val)
        else:
            raise RuntimeError('Unknown doc ref tag %r' % tag)

    def _format_type_in_doc(self, namespace, data_type):
        """
        Returns a string that can be recognized by Sphinx as a type reference
        in a docstring.
        """
        if is_void_type(data_type):
            return 'None'
        elif is_user_defined_type(data_type):
            return ':class:`dropbox.{}.{}`'.format(
                namespace.name, fmt_type(data_type))
        else:
            return fmt_type(data_type)

    def _generate_python_value(self, namespace, value):
        if is_tag_ref(value):
            return '{}.{}.{}'.format(
                namespace.name,
                class_name_for_data_type(value.union_data_type),
                fmt_var(value.tag_name))
        else:
            return fmt_obj(value)
