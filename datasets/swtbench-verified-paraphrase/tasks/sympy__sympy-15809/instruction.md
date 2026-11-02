Interpret <issue/> as a request to make the reported behavior reproducible in automated tests. The implementation solution is outside the scope of this task.
<issue>
      Zero-argument Min() and Max()
      Right now `Min()` and `Max()` with no arguments raise `ValueError: The Max/Min functions must have arguments.`. It might be mathematically more convenient to have them return `oo` and `-oo`, respectively. See https://en.wikipedia.org/wiki/Empty_set#Extended_real_numbers for why these are valid answers mathematically.

</issue>
Deliver tests only, with a clear fail-before/pass-after signal for the issue. Do not leave any source-module edits in the final patch.