from __future__ import unicode_literals, print_function
from contextlib import contextmanager
from xml.etree import ElementTree as etree
from xml.sax.saxutils import quoteattr

import six


CNAME_TAGS = ('system-out', 'skipped', 'error', 'failure')
CNAME_PATTERN = '<![CDATA[{}]]>'
TAG_PATTERN = '<{tag}{attrs}>{text}</{tag}>'


@contextmanager
def patch_etree_cname(etree):
    """
    Patch ElementTree's _serialize_xml function so that it will
    write text as CDATA tag for tags tags defined in CNAME_TAGS.

    >>> import re
    >>> from xml.etree import ElementTree
    >>> xml_string = '''
    ... <testsuite name="nosetests" tests="1" errors="0" failures="0" skip="0">
    ...     <testcase classname="some.class.Foo" name="test_system_out" time="0.001">
    ...         <system-out>Some output here</system-out>
    ...     </testcase>
    ...     <testcase classname="some.class.Foo" name="test_skipped" time="0.001">
    ...         <skipped type="unittest.case.SkipTest" message="Skipped">Skipped</skipped>
    ...     </testcase>
    ...     <testcase classname="some.class.Foo" name="test_error" time="0.001">
    ...         <error type="KeyError" message="Error here">Error here</error>
    ...     </testcase>
    ...     <testcase classname="some.class.Foo" name="test_failure" time="0.001">
    ...         <failure type="AssertionError" message="Failure here">Failure here</failure>
    ...     </testcase>
    ... </testsuite>
    ... '''
    >>> tree = ElementTree.fromstring(xml_string)
    >>> with patch_etree_cname(ElementTree):
    ...    saved = str(ElementTree.tostring(tree))
    >>> systemout = re.findall(r'(<system-out>.*?</system-out>)', saved)[0]
    >>> print(systemout)
    <system-out><![CDATA[Some output here]]></system-out>
    >>> skipped = re.findall(r'(<skipped.*?</skipped>)', saved)[0]
    >>> print(skipped)
    <skipped message="Skipped" type="unittest.case.SkipTest"><![CDATA[Skipped]]></skipped>
    >>> error = re.findall(r'(<error.*?</error>)', saved)[0]
    >>> print(error)
    <error message="Error here" type="KeyError"><![CDATA[Error here]]></error>
    >>> failure = re.findall(r'(<failure.*?</failure>)', saved)[0]
    >>> print(failure)
    <failure message="Failure here" type="AssertionError"><![CDATA[Failure here]]></failure>
    """
    original_serialize = etree._serialize_xml

    def _serialize_xml(write, elem, *args, **kwargs):
        if elem.tag in CNAME_TAGS:
            attrs = ' '.join(
                ['{}={}'.format(k, quoteattr(v))
                 for k, v in sorted(elem.attrib.items())]
            )
            attrs = ' ' + attrs if attrs else ''
            text = CNAME_PATTERN.format(elem.text)
            write(TAG_PATTERN.format(
                tag=elem.tag,
                attrs=attrs,
                text=text
            ).encode('utf-8'))
        else:
            original_serialize(write, elem, *args, **kwargs)

    etree._serialize_xml = etree._serialize['xml'] = _serialize_xml

    yield

    etree._serialize_xml = etree._serialize['xml'] = original_serialize


def is_test_state(xml_element, state):
    for i in xml_element.iter(state):
        return True
    return False


def is_test_skipped(xml_element):
    return is_test_state(xml_element, 'skipped')


def test_get_name(xml_element):
    return xml_element.attrib['name']


def test_suite_update_attribs(test_suite):
    suite_state = {
        "tests": 0,
        "skipped": 0,
        "failures": 0,
        "errors": 0,
        "time": 0.0,
    }

    for test in test_suite.iter('testcase'):
        suite_state['time'] += float(test.attrib.get('time', '0'))

        if is_test_state(test, 'skipped'):
            suite_state['skipped'] += 1
        else:
            suite_state['tests'] += 1
            if is_test_state(test, 'failure'):
                suite_state['failures'] += 1
            elif is_test_state(test, 'error'):
                suite_state['errors'] += 1

    for key in suite_state:
        test_suite.set(key, six.text_type(suite_state[key]))

    return test_suite


def merge_trees(*trees):
    """
    Merge all given XUnit ElementTrees into a single ElementTree.
    This combines all of the children test-cases and also merges
    all of the metadata of how many tests were executed, etc.
    """
    first_tree = trees[0]
    first_root = first_tree.getroot()

    if len(trees) <= 1:
        return first_tree

    # Tracking of skipped and completed tests allow us to handle
    # override skipped test result by completed test result
    skipped_tests = dict()
    completed_tests = []

    # remove skipped tests from first tree
    # and fill tracks of completed and skipped tests
    for test in first_root.findall("testcase"):
        test_name = test_get_name(test)

        if is_test_skipped(test):
            first_root.remove(test)
            if test_name not in skipped_tests:
                skipped_tests[test_name] = test
        else:
            if test_name not in completed_tests:
                completed_tests.append(test_name)

    # cycle over others trees
    for tree in trees[1:]:
        root = tree.getroot()

        for test in root:
            # for every testcase add it to first_root if it not skipped
            # else add it to skipped_tests dict

            test_name = test_get_name(test)
            if is_test_skipped(test):
                if test_name not in completed_tests and test_name not in skipped_tests:
                    skipped_tests[test_name] = test
            else:
                if test_name in skipped_tests:
                    # overriding of previously skipped test
                    del skipped_tests[test_name]
                    if test_name in skipped_tests:
                        print("not removed?")
                if test_name in completed_tests:
                    print("WARNING: Duplication of completed '{}' test case".format(test_name))
                else:
                    completed_tests.append(test_name)
                first_root.append(test)

    # add skipped tests that stored separately
    for test_name in skipped_tests:
        if test_name not in completed_tests:
            first_root.append(skipped_tests[test_name])

    # update attributes of testsuite tag
    test_suite_update_attribs(first_root)

    return first_tree


def merge_xunit(files, output, callback=None):
    """
    Merge the given xunit xml files into a single output xml file.

    If callback is not None, it will be called with the merged ElementTree
    before the output file is written (useful for applying other fixes to
    the merged file). This can either modify the element tree in place (and
    return None) or return a completely new ElementTree to be written.
    """
    trees = []

    for f in files:
        trees.append(etree.parse(f))

    merged = merge_trees(*trees)

    if callback is not None:
        result = callback(merged)
        if result is not None:
            merged = result

    with patch_etree_cname(etree):
        merged.write(output, encoding='utf-8', xml_declaration=True)
