"""Agent execution identity and delegated authority (#5028).

Three separate mechanisms live here, and keeping them separate is the point:

- :mod:`run_credential` — *who is calling*. A credential minted at trusted
  dispatch and bound to one invocation and one attempt of it.
- :mod:`grants` — *what that caller may do*. A delegated grant whose authority
  traces to a real human authorization event.
- :mod:`envelope` — *what the gateway authorized on this request*. A short-lived
  signed statement a worker listener can verify without any database access.

None of the three is derivable from the others. A worker holding a valid
credential still needs a grant; a grant still yields no envelope without the
gateway's signing key.
"""
