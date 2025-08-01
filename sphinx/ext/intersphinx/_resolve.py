"""This module provides logic for resolving references to intersphinx targets."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, cast

from docutils import nodes

from sphinx.addnodes import pending_xref
from sphinx.deprecation import _deprecation_warning
from sphinx.errors import ExtensionError
from sphinx.ext.intersphinx._shared import LOGGER, InventoryAdapter
from sphinx.locale import _, __
from sphinx.transforms.post_transforms import ReferencesResolver
from sphinx.util.docutils import CustomReSTDispatcher, SphinxRole
from sphinx.util.osutil import _relative_path

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence, Set
    from types import ModuleType
    from typing import Any

    from docutils.nodes import Node, TextElement, system_message
    from docutils.utils import Reporter

    from sphinx.application import Sphinx
    from sphinx.domains import Domain
    from sphinx.domains._domains_container import _DomainsContainer
    from sphinx.environment import BuildEnvironment
    from sphinx.ext.intersphinx._shared import InventoryName
    from sphinx.util.inventory import _InventoryItem
    from sphinx.util.typing import Inventory, RoleFunction


def _create_element_from_result(
    domain_name: str,
    inv_name: InventoryName | None,
    inv_item: _InventoryItem,
    node: pending_xref,
    contnode: TextElement,
) -> nodes.reference:
    uri = inv_item.uri
    if '://' not in uri and node.get('refdoc'):
        # get correct path in case of subdirectories
        uri = (_relative_path(Path(), Path(node['refdoc']).parent) / uri).as_posix()
    if inv_item.project_version:
        if not inv_item.project_version[0].isdigit():
            # Do not append 'v' to non-numeric version
            version = inv_item.project_version
        else:
            version = f'v{inv_item.project_version}'
        reftitle = _('(in %s %s)') % (inv_item.project_name, version)
    else:
        reftitle = _('(in %s)') % (inv_item.project_name,)

    newnode = nodes.reference('', '', internal=False, refuri=uri, reftitle=reftitle)
    if node.get('refexplicit'):
        # use whatever title was given
        newnode.append(contnode)
    elif inv_item.display_name == '-' or (
        domain_name == 'std' and node['reftype'] == 'keyword'
    ):
        # use whatever title was given, but strip prefix
        title = contnode.astext()
        if inv_name is not None and title.startswith(inv_name + ':'):
            newnode.append(
                contnode.__class__(
                    title[len(inv_name) + 1 :], title[len(inv_name) + 1 :]
                )
            )
        else:
            newnode.append(contnode)
    else:
        # else use the given display name (used for :ref:)
        newnode.append(contnode.__class__(inv_item.display_name, inv_item.display_name))
    return newnode


def _resolve_reference_in_domain_by_target(
    inv_name: InventoryName | None,
    inventory: Inventory,
    domain_name: str,
    objtypes: Iterable[str],
    target: str,
    node: pending_xref,
    contnode: TextElement,
) -> nodes.reference | None:
    for objtype in objtypes:
        if objtype not in inventory:
            # Continue if there's nothing of this kind in the inventory
            continue

        if target in inventory[objtype]:
            # Case sensitive match, use it
            data = inventory[objtype][target]
        elif objtype in {'std:label', 'std:term'}:
            # Some types require case insensitive matches:
            # * 'term': https://github.com/sphinx-doc/sphinx/issues/9291
            # * 'label': https://github.com/sphinx-doc/sphinx/issues/12008
            target_lower = target.lower()
            insensitive_matches = list(
                filter(lambda k: k.lower() == target_lower, inventory[objtype].keys())
            )
            if len(insensitive_matches) > 1:
                data_items = {
                    inventory[objtype][match] for match in insensitive_matches
                }
                inv_descriptor = inv_name or 'main_inventory'
                if len(data_items) == 1:  # these are duplicates; relatively innocuous
                    LOGGER.debug(
                        __("inventory '%s': duplicate matches found for %s:%s"),
                        inv_descriptor,
                        objtype,
                        target,
                        type='intersphinx',
                        subtype='external',
                        location=node,
                    )
                else:
                    LOGGER.warning(
                        __("inventory '%s': multiple matches found for %s:%s"),
                        inv_descriptor,
                        objtype,
                        target,
                        type='intersphinx',
                        subtype='external',
                        location=node,
                    )
            if insensitive_matches:
                data = inventory[objtype][insensitive_matches[0]]
            else:
                # No case insensitive match either, continue to the next candidate
                continue
        else:
            # Could reach here if we're not a term but have a case insensitive match.
            # This is a fix for terms specifically, but potentially should apply to
            # other types.
            continue
        return _create_element_from_result(domain_name, inv_name, data, node, contnode)
    return None


def _resolve_reference_in_domain(
    inv_name: InventoryName | None,
    inventory: Inventory,
    honor_disabled_refs: bool,
    disabled_reftypes: Set[str],
    domain: Domain,
    objtypes: Iterable[str],
    node: pending_xref,
    contnode: TextElement,
) -> nodes.reference | None:
    domain_name = domain.name
    obj_types: dict[str, None] = dict.fromkeys(objtypes)

    # we adjust the object types for backwards compatibility
    if domain_name == 'std' and 'cmdoption' in obj_types:
        # cmdoptions were stored as std:option until Sphinx 1.6
        obj_types['option'] = None
    if domain_name == 'py' and 'attribute' in obj_types:
        # properties are stored as py:method since Sphinx 2.1
        obj_types['method'] = None

    # the inventory contains domain:type as objtype
    obj_types = {f'{domain_name}:{obj_type}': None for obj_type in obj_types}

    # now that the objtypes list is complete we can remove the disabled ones
    if honor_disabled_refs:
        obj_types = {
            obj_type: None
            for obj_type in obj_types
            if obj_type not in disabled_reftypes
        }

    objtypes = [*obj_types.keys()]

    # without qualification
    res = _resolve_reference_in_domain_by_target(
        inv_name, inventory, domain_name, objtypes, node['reftarget'], node, contnode
    )
    if res is not None:
        return res

    # try with qualification of the current scope instead
    full_qualified_name = domain.get_full_qualified_name(node)
    if full_qualified_name is None:
        return None
    return _resolve_reference_in_domain_by_target(
        inv_name, inventory, domain_name, objtypes, full_qualified_name, node, contnode
    )


def _resolve_reference(
    inv_name: InventoryName | None,
    domains: _DomainsContainer,
    inventory: Inventory,
    honor_disabled_refs: bool,
    disabled_reftypes: Set[str],
    node: pending_xref,
    contnode: TextElement,
) -> nodes.reference | None:
    # disabling should only be done if no inventory is given
    honor_disabled_refs = honor_disabled_refs and inv_name is None

    if honor_disabled_refs and '*' in disabled_reftypes:
        return None

    typ = node['reftype']
    if typ == 'any':
        for domain in domains.sorted():
            if honor_disabled_refs and f'{domain.name}:*' in disabled_reftypes:
                continue
            objtypes: Iterable[str] = domain.object_types.keys()
            res = _resolve_reference_in_domain(
                inv_name,
                inventory,
                honor_disabled_refs,
                disabled_reftypes,
                domain,
                objtypes,
                node,
                contnode,
            )
            if res is not None:
                return res
        return None
    else:
        domain_name = node.get('refdomain')
        if not domain_name:
            # only objects in domains are in the inventory
            return None
        if honor_disabled_refs and f'{domain_name}:*' in disabled_reftypes:
            return None
        try:
            domain = domains[domain_name]
        except KeyError as exc:
            msg = __('Domain %r is not registered') % domain_name
            raise ExtensionError(msg) from exc

        objtypes = domain.objtypes_for_role(typ) or ()
        if not objtypes:
            return None
        return _resolve_reference_in_domain(
            inv_name,
            inventory,
            honor_disabled_refs,
            disabled_reftypes,
            domain,
            objtypes,
            node,
            contnode,
        )


def inventory_exists(env: BuildEnvironment, inv_name: InventoryName) -> bool:
    return inv_name in InventoryAdapter(env).named_inventory


def resolve_reference_in_inventory(
    env: BuildEnvironment,
    inv_name: InventoryName,
    node: pending_xref,
    contnode: TextElement,
) -> nodes.reference | None:
    """Attempt to resolve a missing reference via intersphinx references.

    Resolution is tried in the given inventory with the target as is.

    Requires ``inventory_exists(env, inv_name)``.
    """
    assert inventory_exists(env, inv_name)
    return _resolve_reference(
        inv_name,
        env.domains,
        InventoryAdapter(env).named_inventory[inv_name],
        False,
        frozenset(env.config.intersphinx_disabled_reftypes),
        node,
        contnode,
    )


def resolve_reference_any_inventory(
    env: BuildEnvironment,
    honor_disabled_refs: bool,
    node: pending_xref,
    contnode: TextElement,
) -> nodes.reference | None:
    """Attempt to resolve a missing reference via intersphinx references.

    Resolution is tried with the target as is in any inventory.
    """
    return _resolve_reference(
        None,
        env.domains,
        InventoryAdapter(env).main_inventory,
        honor_disabled_refs,
        frozenset(env.config.intersphinx_disabled_reftypes),
        node,
        contnode,
    )


def resolve_reference_detect_inventory(
    env: BuildEnvironment, node: pending_xref, contnode: TextElement
) -> nodes.reference | None:
    """Attempt to resolve a missing reference via intersphinx references.

    Resolution is tried first with the target as is in any inventory.
    If this does not succeed, then the target is split by the first ``:``,
    to form ``inv_name:new_target``. If ``inv_name`` is a named inventory, then resolution
    is tried in that inventory with the new target.
    """
    resolve_self = env.config.intersphinx_resolve_self

    # ordinary direct lookup, use data as is
    res = resolve_reference_any_inventory(env, True, node, contnode)
    if res is not None:
        return res

    # try splitting the target into 'inv_name:target'
    target = node['reftarget']
    if ':' not in target:
        return None
    inv_name, _, new_target = target.partition(':')

    # check if the target is self-referential
    self_referential = bool(resolve_self) and resolve_self == inv_name
    if self_referential:
        node['reftarget'] = new_target
        node['intersphinx_self_referential'] = True
        return None

    if not inventory_exists(env, inv_name):
        return None
    node['reftarget'] = new_target
    res_inv = resolve_reference_in_inventory(env, inv_name, node, contnode)
    node['reftarget'] = target
    return res_inv


def missing_reference(
    app: Sphinx, env: BuildEnvironment, node: pending_xref, contnode: TextElement
) -> nodes.reference | None:
    """Attempt to resolve a missing reference via intersphinx references."""
    return resolve_reference_detect_inventory(env, node, contnode)


class IntersphinxDispatcher(CustomReSTDispatcher):
    """Custom dispatcher for external role.

    This enables :external:***:/:external+***: roles on parsing reST document.
    """

    def role(
        self,
        role_name: str,
        language_module: ModuleType,
        lineno: int,
        reporter: Reporter,
    ) -> tuple[RoleFunction, list[system_message]]:
        if len(role_name) > 9 and role_name.startswith(('external:', 'external+')):
            return IntersphinxRole(role_name), []
        else:
            return super().role(role_name, language_module, lineno, reporter)


class IntersphinxRole(SphinxRole):
    # group 1: just for the optionality of the inventory name
    # group 2: the inventory name (optional)
    # group 3: the domain:role or role part
    _re_inv_ref = re.compile(r'(\+([^:]+))?:(.*)')

    def __init__(self, orig_name: str) -> None:
        self.orig_name = orig_name

    def run(self) -> tuple[list[Node], list[system_message]]:
        assert self.name == self.orig_name.lower()
        inventory, name_suffix = self.get_inventory_and_name_suffix(self.orig_name)
        resolve_self = self.env.config.intersphinx_resolve_self
        self_referential = bool(resolve_self) and resolve_self == inventory

        if not self_referential:
            if inventory and not inventory_exists(self.env, inventory):
                self._emit_warning(
                    __('inventory for external cross-reference not found: %r'),
                    inventory,
                )
                return [], []

        domain_name, role_name = self._get_domain_role(name_suffix)

        if role_name is None:
            self._emit_warning(
                __('invalid external cross-reference suffix: %r'), name_suffix
            )
            return [], []

        # attempt to find a matching role function
        role_func: RoleFunction | None

        if domain_name is not None:
            # the user specified a domain, so we only check that
            if domain_name not in self.env.domains:
                self._emit_warning(
                    __('domain for external cross-reference not found: %r'), domain_name
                )
                return [], []
            domain = self.env.domains[domain_name]
            role_func = domain.roles.get(role_name)
            if role_func is None:
                msg = 'role for external cross-reference not found in domain %r: %r'
                object_types = domain.object_types.get(role_name)
                if object_types is not None and object_types.roles:
                    self._emit_warning(
                        __(f'{msg} (perhaps you meant one of: %s)'),
                        domain_name,
                        role_name,
                        self._concat_strings(object_types.roles),
                    )
                else:
                    self._emit_warning(__(msg), domain_name, role_name)
                return [], []

        else:
            # the user did not specify a domain,
            # so we check first the default (if available) then standard domains
            default_domain = self.env.current_document.default_domain
            std_domain = self.env.domains.standard_domain
            domains: Sequence[Domain]
            if default_domain is None or std_domain == default_domain:
                domains = (std_domain,)
            else:
                domains = (default_domain, std_domain)

            role_func = None
            for domain in domains:
                role_func = domain.roles.get(role_name)
                if role_func is not None:
                    domain_name = domain.name
                    break

            if role_func is None or domain_name is None:
                domains_str = self._concat_strings(domain.name for domain in domains)
                msg = 'role for external cross-reference not found in domains %s: %r'
                possible_roles: set[str] = {
                    f'{domain.name}:{r}'
                    for domain in domains
                    if (object_types := domain.object_types.get(role_name))
                    for r in object_types.roles
                }
                if possible_roles:
                    msg = f'{msg} (perhaps you meant one of: %s)'
                    self._emit_warning(
                        __(msg),
                        domains_str,
                        role_name,
                        self._concat_strings(possible_roles),
                    )
                else:
                    self._emit_warning(__(msg), domains_str, role_name)
                return [], []

        result, messages = role_func(
            f'{domain_name}:{role_name}',
            self.rawtext,
            self.text,
            self.lineno,
            self.inliner,
            self.options,
            self.content,
        )

        if not self_referential:
            # We do the intersphinx resolution by inserting our
            # 'intersphinx' and 'inventory' attributes into the nodes.
            # Only do this when it is an external reference.
            for node in result:
                if isinstance(node, pending_xref):
                    node['intersphinx'] = True
                    node['inventory'] = inventory

        return result, messages

    def get_inventory_and_name_suffix(self, name: str) -> tuple[str | None, str]:
        """Extract an inventory name (if any) and ``domain+name`` suffix from a role *name*.
        and the domain+name suffix.

        The role name is expected to be of one of the following forms:

        - ``external+inv:name`` -- explicit inventory and name, any domain.
        - ``external+inv:domain:name`` -- explicit inventory, domain and name.
        - ``external:name`` -- any inventory and domain, explicit name.
        - ``external:domain:name`` -- any inventory, explicit domain and name.
        """
        assert name.startswith('external'), name
        suffix = name[9:]
        if name[8] == '+':
            inv_name, _, suffix = suffix.partition(':')
            return inv_name, suffix
        elif name[8] == ':':
            return None, suffix
        else:
            msg = f'Malformed :external: role name: {name}'
            raise ValueError(msg)

    def _get_domain_role(self, name: str) -> tuple[str | None, str | None]:
        """Convert the *name* string into a domain and a role name.

        - If *name* contains no ``:``, return ``(None, name)``.
        - If *name* contains a single ``:``, the domain/role is split on this.
        - If *name* contains multiple ``:``, return ``(None, None)``.
        """
        names = name.split(':')
        if len(names) == 1:
            return None, names[0]
        elif len(names) == 2:
            return names[0], names[1]
        else:
            return None, None

    def _emit_warning(self, msg: str, /, *args: Any) -> None:
        LOGGER.warning(
            msg,
            *args,
            type='intersphinx',
            subtype='external',
            location=(self.env.current_document.docname, self.lineno),
        )

    def _concat_strings(self, strings: Iterable[str]) -> str:
        return ', '.join(f'{s!r}' for s in sorted(strings))

    # deprecated methods

    def get_role_name(self, name: str) -> tuple[str, str] | None:
        _deprecation_warning(
            __name__, f'{self.__class__.__name__}.get_role_name', '', remove=(9, 0)
        )
        names = name.split(':')
        if len(names) == 1:
            # role
            if (domain := self.env.current_document.default_domain) is not None:
                domain_name = domain.name
            else:
                domain_name = None
            role = names[0]
        elif len(names) == 2:
            # domain:role:
            domain_name, role = names
        else:
            return None

        if domain_name and self.is_existent_role(domain_name, role):
            return domain_name, role
        elif self.is_existent_role('std', role):
            return 'std', role
        else:
            return None

    def is_existent_role(self, domain_name: str, role_name: str) -> bool:
        _deprecation_warning(
            __name__, f'{self.__class__.__name__}.is_existent_role', '', remove=(9, 0)
        )
        try:
            domain = self.env.domains[domain_name]
        except KeyError:
            return False
        else:
            return role_name in domain.roles

    def invoke_role(
        self, role: tuple[str, str]
    ) -> tuple[list[Node], list[system_message]]:
        """Invoke the role described by a ``(domain, role name)`` pair."""
        _deprecation_warning(
            __name__, f'{self.__class__.__name__}.invoke_role', '', remove=(9, 0)
        )
        domain = self.env.get_domain(role[0])
        if domain:
            role_func = domain.role(role[1])
            assert role_func is not None

            return role_func(
                ':'.join(role),
                self.rawtext,
                self.text,
                self.lineno,
                self.inliner,
                self.options,
                self.content,
            )
        else:
            return [], []


class IntersphinxRoleResolver(ReferencesResolver):
    """pending_xref node resolver for intersphinx role.

    This resolves pending_xref nodes generated by :intersphinx:***: role.
    """

    default_priority = ReferencesResolver.default_priority - 1

    def run(self, **kwargs: Any) -> None:
        for node in self.document.findall(pending_xref):
            if 'intersphinx' not in node:
                continue
            contnode = cast('nodes.TextElement', node[0].deepcopy())
            inv_name = node['inventory']
            if inv_name is not None:
                assert inventory_exists(self.env, inv_name)
                newnode = resolve_reference_in_inventory(
                    self.env, inv_name, node, contnode
                )
            else:
                newnode = resolve_reference_any_inventory(
                    self.env, False, node, contnode
                )
            if newnode is None:
                typ = node['reftype']
                msg = __('external %s:%s reference target not found: %s') % (
                    node['refdomain'],
                    typ,
                    node['reftarget'],
                )
                LOGGER.warning(msg, location=node, type='ref', subtype=typ)
                node.replace_self(contnode)
            else:
                node.replace_self(newnode)


def install_dispatcher(app: Sphinx, docname: str, source: list[str]) -> None:
    """Enable IntersphinxDispatcher.

    .. note:: The installed dispatcher will be uninstalled on disabling sphinx_domain
              automatically.
    """
    dispatcher = IntersphinxDispatcher()
    dispatcher.enable()
