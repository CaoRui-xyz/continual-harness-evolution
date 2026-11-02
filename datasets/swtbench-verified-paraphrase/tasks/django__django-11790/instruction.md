Use <issue/> as the specification for a regression check. Extend the test suite to capture the report, while keeping production source untouched.
<issue>
      AuthenticationForm's username field doesn't set maxlength HTML attribute.
      Description

      AuthenticationForm's username field doesn't render with maxlength HTML attribute anymore.
      Regression introduced in #27515 and 5ceaf14686ce626404afb6a5fbd3d8286410bf13.
      ​https://groups.google.com/forum/?utm_source=digest&utm_medium=email#!topic/django-developers/qnfSqro0DlA
      ​https://forum.djangoproject.com/t/possible-authenticationform-max-length-regression-in-django-2-1/241

</issue>
The final patch should contain tests rather than an implementation solution. Its assertions must differentiate the current defect from the corrected result.