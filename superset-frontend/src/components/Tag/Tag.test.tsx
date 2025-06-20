/**
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 * KIND, either express or implied.  See the License for the
 * specific language governing permissions and limitations
 * under the License.
 */
import { render, screen } from 'spec/helpers/testing-library';
import TagType from 'src/types/TagType';
import { Tag } from '.';

const mockedProps: TagType = {
  name: 'example-tag',
  id: 1,
  onDelete: undefined,
  editable: false,
  onClick: undefined,
};

const setup = (props: TagType = mockedProps) =>
  render(<Tag {...props} />, { useRouter: true });

test('should render', () => {
  const { container } = setup();
  expect(container).toBeInTheDocument();
});

test('should render shortname properly', () => {
  const { container } = setup();
  expect(container).toBeInTheDocument();
  expect(screen.getByTestId('tag')).toBeInTheDocument();
  expect(screen.getByTestId('tag')).toHaveTextContent(mockedProps.name || '');
});

test('should render longname properly', () => {
  const longNameProps = {
    ...mockedProps,
    name: 'very-long-tag-name-that-truncates',
  };
  const { container } = setup(longNameProps);
  expect(container).toBeInTheDocument();
  expect(screen.getByTestId('tag')).toBeInTheDocument();
  expect(screen.getByTestId('tag')).toHaveTextContent(
    `${longNameProps.name.slice(0, 20)}...`,
  );
});
