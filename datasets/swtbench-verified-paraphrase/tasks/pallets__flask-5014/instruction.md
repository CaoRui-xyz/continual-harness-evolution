Use the issue text inside <issue/> to design regression coverage. The desired outcome is a testing patch; do not retain a source-code solution.
<issue>
      Require a non-empty name for Blueprints
      Things do not work correctly if a Blueprint is given an empty name (e.g. #4944).
      It would be helpful if a `ValueError` was raised when trying to do that.

</issue>
Place clear checks in the appropriate existing suite. They should fail on the defective version and pass once the reported behavior is corrected.