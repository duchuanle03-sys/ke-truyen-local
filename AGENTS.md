# Maintenance

- Before committing, check for implementations superseded by the change and remove code, tests, fixtures, configuration and documentation that are no longer used.
- Confirm usage across application callers, documented tools, dynamic references and tests before deleting. Tests alone do not justify keeping an obsolete implementation.
- Preserve user libraries, imported files, dictionaries, backups and private browser profiles when cleaning source code.
- After every local commit, immediately push to the configured GitHub remote and branch unless the user explicitly requests otherwise.
