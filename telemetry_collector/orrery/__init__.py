"""Orrery artifacts the collector writes from a build's submitted inputs.

``projection`` turns a build's saved compiler schema and execution overlay into
Orrery JSON, and ``validation`` validates that JSON with the pinned
``@axiom-foundation/orrery``. The worker that publishes it is not part of this
package yet.
"""
