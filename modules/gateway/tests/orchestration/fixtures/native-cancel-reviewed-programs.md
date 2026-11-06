# Reviewed native cancellation regression fixture

`native-cancel-reviewed-programs.zip` contains the six unmodified publication
programs from commit `76fb417e4a8a701dbeb459b880694cfe3d38764b`. Their ordered
path/content digest is
`56084b3744e20bc7ed5b2f9e4f167dcf0508cbc3b14eb435131c9acae37d66a2`, the exact
release recognized by the existing cancellation verifier.

The provider fixture models a read at an immutable historical dispatch revision.
Reading the current checkout instead substituted later publication programs into
that old dispatch and made unrelated PRs fail after the adapter publisher changed.
The archive is read as data and is never extracted or executed.

The tests verify the complete archive against the production verifier's digest
and reject a change to each of its six members. This does not authorize newer
publication implementations or claim that the legacy verifier can recover them;
unknown releases continue to fail closed. It does not change the engine, its
deployment policy, or live acceptance requirements.
