"""Pipeline step functions.

Each step is an independent function with signature::

    def step_xxx(ctx: WorkflowContext, config: TAConfig) -> WorkflowContext

Steps read from *ctx*, perform their work, and return an updated
``WorkflowContext`` (never mutating the input).
"""
